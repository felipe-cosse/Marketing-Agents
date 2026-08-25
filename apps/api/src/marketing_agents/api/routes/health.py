"""Process-only liveness and fail-closed traffic readiness endpoints."""

from __future__ import annotations

import asyncio
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from marketing_agents.api.dependencies import get_readiness_probe
from marketing_agents.application.ports.readiness import (
    CatalogReadinessMetadata,
    ReadinessCheck,
    ReadinessCheckName,
    ReadinessCheckStatus,
    ReadinessCode,
    ReadinessProbe,
    ReadinessReport,
    unavailable_readiness_report,
)

READINESS_PROBE_TIMEOUT_SECONDS = 5.0
_NO_STORE = "no-store"


class LiveHealth(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ok"] = "ok"
    service: Literal["marketing-agents-api"] = "marketing-agents-api"


class ReadinessCheckHealth(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: ReadinessCheckName
    status: ReadinessCheckStatus
    code: ReadinessCode


class CatalogHealth(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    content_version: str = Field(min_length=1, max_length=64)
    content_hash: str = Field(pattern=r"^catalog-sha256-v1:[a-f0-9]{64}$")
    departments: Literal[5]
    functions: Literal[12]
    templates: Literal[36]
    instances: Literal[43]


class ReadyHealth(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ready", "not_ready"]
    service: Literal["marketing-agents-api"] = "marketing-agents-api"
    checks: tuple[ReadinessCheckHealth, ...] = Field(min_length=6, max_length=6)
    catalog: CatalogHealth | None = None


router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live", response_model=LiveHealth, operation_id="getHealthLiveness")
def liveness(response: Response) -> LiveHealth:
    response.headers["Cache-Control"] = _NO_STORE
    return LiveHealth()


def _project_check(check: ReadinessCheck) -> ReadinessCheckHealth:
    return ReadinessCheckHealth(
        name=check.name.value,
        status=check.status.value,
        code=check.code.value,
    )


def _project_catalog(catalog: CatalogReadinessMetadata | None) -> CatalogHealth | None:
    if catalog is None:
        return None
    return CatalogHealth(
        content_version=catalog.content_version,
        content_hash=catalog.content_hash,
        departments=catalog.departments,
        functions=catalog.functions,
        templates=catalog.templates,
        instances=catalog.instances,
    )


def _project_report(report: ReadinessReport) -> ReadyHealth:
    return ReadyHealth(
        status="ready" if report.ready else "not_ready",
        checks=tuple(_project_check(check) for check in report.checks),
        catalog=_project_catalog(report.catalog),
    )


async def _safe_report(probe: ReadinessProbe | None) -> ReadinessReport:
    if probe is None:
        return unavailable_readiness_report(ReadinessCode.READINESS_UNAVAILABLE)
    try:
        report = await asyncio.wait_for(
            probe.check(),
            timeout=READINESS_PROBE_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        return unavailable_readiness_report(ReadinessCode.READINESS_TIMEOUT)
    except Exception:
        return unavailable_readiness_report(ReadinessCode.READINESS_UNAVAILABLE)
    if type(report) is not ReadinessReport:
        return unavailable_readiness_report(ReadinessCode.READINESS_UNAVAILABLE)
    try:
        return ReadinessReport(checks=report.checks, catalog=report.catalog)
    except (TypeError, ValueError):
        return unavailable_readiness_report(ReadinessCode.READINESS_UNAVAILABLE)


@router.get(
    "/ready",
    response_model=ReadyHealth,
    operation_id="getHealthReadiness",
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ReadyHealth,
            "description": "One or more internal readiness checks are not ready.",
        }
    },
)
async def readiness(
    probe: Annotated[ReadinessProbe | None, Depends(get_readiness_probe)],
) -> JSONResponse:
    report = await _safe_report(probe)
    projected = _project_report(report)
    status_code = status.HTTP_200_OK if report.ready else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(
        status_code=status_code,
        content=projected.model_dump(mode="json"),
        headers={"Cache-Control": _NO_STORE},
    )

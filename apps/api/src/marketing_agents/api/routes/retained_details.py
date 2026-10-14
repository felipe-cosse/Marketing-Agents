"""Read-only retained identities; never expose sealed bindings or deleted detail."""

from datetime import datetime
from typing import Annotated, Literal, Protocol, cast

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from pydantic import BaseModel, ConfigDict

from marketing_agents.api.dependencies import require_runtime_resource_reader_principal
from marketing_agents.application.policies.runtime_resource_authorization import (
    RuntimeResourceAuthorizationError,
)
from marketing_agents.domain.identity import AuthenticatedPrincipal
from marketing_agents.domain.retained_details import RetainedDetail
from marketing_agents.domain.retention import RetentionCategory

RecordKind = Literal[
    "work", "action", "approval", "artifact", "receipt", "attempt", "audit", "brief"
]
_HEADERS = {
    "Cache-Control": "no-store",
    "Vary": "Authorization",
    "X-Content-Type-Options": "nosniff",
}
router = APIRouter(prefix="/api/v1/retained-details", tags=["retention"])


class RetainedResourceExecutor(Protocol):
    async def read(
        self, *, principal: AuthenticatedPrincipal, record_kind: str, record_id: str
    ) -> RetainedDetail | None: ...


class RetainedIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    record_kind: RecordKind
    record_id: str
    category: RetentionCategory
    run_id: str | None
    created_at: datetime
    expires_at: datetime
    purged_at: datetime
    detail_expired: Literal[True] = True


def get_retained_resource_executor(request: Request) -> RetainedResourceExecutor:
    service = getattr(request.app.state, "retained_resource_service", None)
    if not callable(getattr(service, "read", None)):
        raise HTTPException(503, headers=_HEADERS)
    return cast(RetainedResourceExecutor, service)


@router.get("/{record_kind}/{record_id}", response_model=RetainedIdentity)
async def read_retained_identity(
    record_kind: RecordKind,
    record_id: Annotated[str, Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,239}$")],
    response: Response,
    principal: Annotated[
        AuthenticatedPrincipal, Depends(require_runtime_resource_reader_principal)
    ],
    service: Annotated[RetainedResourceExecutor, Depends(get_retained_resource_executor)],
) -> RetainedIdentity:
    try:
        retained = await service.read(
            principal=principal, record_kind=record_kind, record_id=record_id
        )
        if retained is None:
            raise HTTPException(404, headers=_HEADERS)
        if (
            type(retained) is not RetainedDetail
            or retained.record_id != record_id
            or retained.record_kind != record_kind
        ):
            raise ValueError("retained identity mismatch")
        result = RetainedIdentity(
            record_kind=record_kind,
            record_id=retained.record_id,
            category=retained.category,
            run_id=retained.run_id,
            created_at=retained.created_at,
            expires_at=retained.expires_at,
            purged_at=retained.purged_at,
        )
    except HTTPException:
        raise
    except RuntimeResourceAuthorizationError:
        raise HTTPException(403, headers=_HEADERS) from None
    except Exception:
        raise HTTPException(503, headers=_HEADERS) from None
    response.headers.update(_HEADERS)
    return result

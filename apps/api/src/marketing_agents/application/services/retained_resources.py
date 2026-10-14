"""Authorized identity-only inspection after physical detail expiry."""

from marketing_agents.application.policies.runtime_resource_authorization import (
    authorize_runtime_resource_reader,
)
from marketing_agents.application.ports.retention import RetentionRepository
from marketing_agents.domain.identity import AuthenticatedPrincipal
from marketing_agents.domain.retained_details import RetainedDetail
from marketing_agents.domain.validation import require_id

RETAINED_RECORD_KINDS = frozenset(
    {"work", "action", "approval", "artifact", "receipt", "attempt", "audit", "brief"}
)


class RetainedResourceService:
    def __init__(self, repository: RetentionRepository) -> None:
        self._repository = repository

    async def read(
        self, *, principal: AuthenticatedPrincipal, record_kind: str, record_id: str
    ) -> RetainedDetail | None:
        authorize_runtime_resource_reader(principal)
        if record_kind not in RETAINED_RECORD_KINDS:
            raise ValueError("retained record kind is invalid")
        require_id(record_id, "retained record ID")
        return await self._repository.inspect(record_kind, record_id)

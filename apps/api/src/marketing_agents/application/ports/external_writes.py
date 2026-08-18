"""Connector-delivery boundary used only after durable authorization claims."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from marketing_agents.application.policies.write_authorization import (
    AuthorizedExternalWrite,
)
from marketing_agents.application.ports.connectors import ConnectorWriteResult
from marketing_agents.domain.entities import ExternalAction


@dataclass(frozen=True, slots=True)
class ConnectorDeliveryContract:
    capability_id: str
    connector_family: str
    binding_id: str
    binding_configuration_revision: int
    request_schema_id: str
    idempotency_support: Literal["required", "supported", "unavailable"]
    timeout_seconds: int


class ConnectorDeliveryFailure(RuntimeError):
    """Safe provider failure with explicit uncertainty classification."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        request_may_have_left_process: bool,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.request_may_have_left_process = request_may_have_left_process


class ExternalWriteConnectorGateway(Protocol):
    def contract_for(self, action: ExternalAction) -> ConnectorDeliveryContract: ...

    async def execute(self, authorization: AuthorizedExternalWrite) -> ConnectorWriteResult: ...

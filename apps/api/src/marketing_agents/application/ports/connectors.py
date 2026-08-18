"""Connector port contracts; mutating operations accept only sealed authorization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pydantic import JsonValue

from marketing_agents.application.policies.write_authorization import AuthorizedExternalWrite


@dataclass(frozen=True)
class ConnectorWriteResult:
    receipt_id: str
    status: str
    safe_metadata: dict[str, JsonValue]


class MutatingConnector(Protocol):
    async def execute(self, write: AuthorizedExternalWrite) -> ConnectorWriteResult:
        """Execute one exact reserved write using its stable idempotency key."""
        ...

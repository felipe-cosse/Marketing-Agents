"""Exact controlled-READ adapter helpers shared by runtime tests."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from marketing_agents.application.ports.read_adapter import (
    ReadAdapterContract,
    ReadAdapterRequest,
    ReadAdapterResult,
)
from marketing_agents.domain.execution_control import OperationExecutionPolicy
from marketing_agents.domain.runtime_policy import AttemptKind


class ExactReadContractAdapter:
    """Test adapter base that accepts only the durable operation projection."""

    def contract_for(self, operation: OperationExecutionPolicy) -> ReadAdapterContract:
        return ReadAdapterContract.from_operation(operation)


def observation_for(
    request: ReadAdapterRequest,
    output_payload: Mapping[str, Any],
    *,
    observation_id: str | None = None,
    model_output_tokens: int | None = None,
) -> ReadAdapterResult:
    """Return a truthful untrusted observation for one exact request."""

    return ReadAdapterResult.from_request(
        request,
        observation_id=observation_id or f"observation:{request.attempt_id}",
        output_payload=output_payload,
        model_output_tokens=(
            1
            if request.attempt_kind is AttemptKind.MODEL and model_output_tokens is None
            else model_output_tokens
        ),
    )


__all__ = ["ExactReadContractAdapter", "observation_for"]

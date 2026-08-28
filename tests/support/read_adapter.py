"""Exact controlled-READ adapter helpers shared by runtime tests."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from marketing_agents.application.ports.read_adapter import (
    ReadAdapterContract,
    ReadAdapterRequest,
    ReadAdapterResult,
)
from marketing_agents.application.ports.runtime_inputs import RuntimeInputContract
from marketing_agents.application.ports.runtime_outputs import RuntimeOutputContract
from marketing_agents.domain.execution_control import OperationExecutionPolicy
from marketing_agents.domain.runtime_policy import AttemptKind


class ExactReadContractAdapter:
    """Test adapter base that accepts only the durable operation projection."""

    def contract_for(self, operation: OperationExecutionPolicy) -> ReadAdapterContract:
        return ReadAdapterContract.from_operation(operation)

    def input_contract_for(
        self,
        operation: OperationExecutionPolicy,
    ) -> RuntimeInputContract:
        if operation.request_schema_id is None:
            raise ValueError("callable test operation requires a request schema")
        return RuntimeInputContract(
            schema_id=operation.request_schema_id,
            schema_version="v1",
            schema={"type": "object"},
            classification=operation.data_classification,
        )

    def output_contract_for(
        self,
        operation: OperationExecutionPolicy,
    ) -> RuntimeOutputContract:
        if operation.result_schema_id is None:
            raise ValueError("callable test operation requires a result schema")
        return RuntimeOutputContract(
            schema_id=operation.result_schema_id,
            schema_version="v1",
            schema={"type": "object"},
            classification=operation.data_classification,
            provider_kind="llm" if operation.kind is AttemptKind.MODEL else "connector",
            provider_mode="mock",
            provider_name=operation.connector_family,
            provider_version="v1",
        )


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

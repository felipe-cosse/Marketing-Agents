"""ORCH-06: generic executor boundaries before any adapter call."""

from __future__ import annotations

import ast
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from marketing_agents.application.orchestration import OrchestrationDependencies
from marketing_agents.application.ports.read_adapter import (
    ReadAdapterContract,
    ReadAdapterRequest,
    ReadAdapterResult,
)
from marketing_agents.application.ports.unit_of_work import UnitOfWorkFactory
from marketing_agents.application.services import (
    ControlledReadCommand,
    ControlledReadExecutor,
    ControlledReadExecutorError,
)
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.data_classification import DataClassification
from marketing_agents.domain.entities import RunStep
from marketing_agents.domain.enums import Effect, StepState
from marketing_agents.domain.execution_control import OperationExecutionPolicy
from marketing_agents.domain.runtime_policy import (
    AttemptKind,
    BudgetPolicySnapshot,
    RateLimitPolicySnapshot,
    RateLimitScope,
    RetryBackoff,
    RetryPolicySnapshot,
    StepRuntimePolicy,
    TimeoutPolicySnapshot,
    runtime_rate_limit_key,
)

from tests.support.read_adapter import ExactReadContractAdapter, observation_for

ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 8, 24, 12, tzinfo=UTC)


class FixedClock:
    def now(self) -> datetime:
        return NOW


class IncrementingIds:
    def __init__(self) -> None:
        self.next_value = 0

    def new(self, namespace: str) -> str:
        self.next_value += 1
        return f"{namespace}.unit.{self.next_value}"


class RecordingAdapter(ExactReadContractAdapter):
    def __init__(self) -> None:
        self.calls: list[ReadAdapterRequest] = []

    async def execute(self, request: ReadAdapterRequest) -> ReadAdapterResult:
        self.calls.append(request)
        return observation_for(request, {"ok": True})


class StepOnlyRepository:
    def __init__(self, step: RunStep) -> None:
        self.step = step

    async def get(self, step_id: str) -> RunStep | None:
        return self.step if step_id == self.step.id else None


class StepOnlyUnitOfWork:
    def __init__(self, step: RunStep) -> None:
        self.run_steps = StepOnlyRepository(step)
        self.commits = 0

    @property
    def execution_control(self) -> object:
        raise AssertionError("rejected step must not reach execution control")

    async def __aenter__(self) -> StepOnlyUnitOfWork:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1


class StepOnlyUnitOfWorkFactory:
    def __init__(self, step: RunStep) -> None:
        self.step = step
        self.instances: list[StepOnlyUnitOfWork] = []

    def __call__(self) -> StepOnlyUnitOfWork:
        instance = StepOnlyUnitOfWork(self.step)
        self.instances.append(instance)
        return instance


def _policy(kind: AttemptKind) -> StepRuntimePolicy:
    return StepRuntimePolicy(
        operation_key="operation.unit.read",
        attempt_kind=kind,
        retry=RetryPolicySnapshot(1, RetryBackoff.NONE),
        timeout=TimeoutPolicySnapshot(10, 30),
        budget=BudgetPolicySnapshot(2, 1, 1),
        rate_limit=RateLimitPolicySnapshot(
            RateLimitScope.TEMPLATE,
            runtime_rate_limit_key(
                template_id="template.unit.read",
                max_calls=2,
                window_seconds=60,
            ),
            2,
            60,
        ),
    )


def _step(*, effect: Effect, kind: AttemptKind) -> RunStep:
    write = effect is Effect.WRITE
    return RunStep(
        id="step.unit.read",
        run_id="run.unit.read",
        key="read",
        kind="connector.read" if write else "model.read",
        selected_instance_id="instance.unit.read",
        dependency_keys=(),
        capability_id="capability.unit.read",
        effect=effect,
        state=StepState.READY,
        plan_hash="a" * 64,
        graph_hash="b" * 64,
        ordinal=1,
        source_order=1,
        template_id="template.unit.read",
        configuration_revision=1,
        connector_family="connector"
        if write
        else ("artifact" if kind is AttemptKind.NO_CALL else "model"),
        routing_slot_key="slot.unit.read" if write else None,
        binding_id="binding.unit.read" if write else None,
        binding_configuration_revision=1 if write else None,
        request_schema_id=(
            "schema.unit.read.request" if write or kind is AttemptKind.MODEL else None
        ),
        result_schema_id=(
            "schema.unit.read.result" if write or kind is AttemptKind.MODEL else None
        ),
        request_redaction_fields=("/secret",) if write else (),
        result_redaction_fields=("/secret",) if write else (),
        data_classification=(DataClassification.PERSONAL if write else DataClassification.INTERNAL),
        idempotency_support="required" if write else "not_applicable",
        timeout_seconds=10 if write else None,
        runtime_policy=_policy(kind),
        approval_policy_id="approval.unit.write" if write else "approval.none",
        approval_required_roles=("approver",) if write else (),
        approval_required_scopes=("approvals:decide",) if write else (),
        approval_expires_after_seconds=60 if write else None,
        approval_allow_self_approval=False if write else None,
        terminal_result=True,
        created_at=NOW,
        updated_at=NOW,
        version=2,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("step", "expected_code"),
    [
        (_step(effect=Effect.WRITE, kind=AttemptKind.TOOL), "read_step_required"),
        (_step(effect=Effect.READ, kind=AttemptKind.NO_CALL), "adapter_call_not_allowed"),
    ],
)
async def test_orch_06_write_and_no_call_steps_are_rejected_before_adapter_or_commit(
    step: RunStep,
    expected_code: str,
) -> None:
    factory = StepOnlyUnitOfWorkFactory(step)
    adapter = RecordingAdapter()
    dependencies = OrchestrationDependencies(
        FixedClock(),
        IncrementingIds(),
        cast(UnitOfWorkFactory, factory),
    )

    with pytest.raises(ControlledReadExecutorError) as captured:
        await ControlledReadExecutor(dependencies, adapter).execute(
            ControlledReadCommand(step.id, {"query": "safe"}),
            audit_context=AuditContext.worker(
                "worker.unit.read",
                correlation_id="correlation.unit.read",
            ),
        )

    assert captured.value.code == expected_code
    assert adapter.calls == []
    assert len(factory.instances) == 1
    assert factory.instances[0].commits == 0


def test_orch_06_adapter_contract_deep_freezes_canonical_payloads() -> None:
    input_payload = {"nested": {"values": [1, 2]}}
    operation = OperationExecutionPolicy(
        run_id="run.unit.read",
        step_id="step.unit.read",
        operation_key="operation.unit.read",
        kind=AttemptKind.MODEL,
        capability_id="capability.unit.read",
        selected_instance_id="instance.unit.read",
        configuration_revision=1,
        connector_family="model",
        binding_id=None,
        binding_configuration_revision=None,
        request_schema_id="schema.unit.read.request",
        result_schema_id="schema.unit.read.result",
        request_redaction_fields=(),
        result_redaction_fields=(),
        data_classification=DataClassification.INTERNAL,
        connector_timeout_seconds=None,
        policy_hash="a" * 64,
        max_attempts=1,
        retry_backoff=RetryBackoff.NONE,
        step_timeout_seconds=10,
        max_input_bytes=65_536,
        max_input_field_bytes=16_384,
        max_output_bytes=262_144,
        max_model_output_tokens=4_096,
        rate_limit_scope=RateLimitScope.TEMPLATE,
        rate_limit_key="rate.unit.read",
        rate_window_max_calls=2,
        rate_window_seconds=60,
    )
    contract = ReadAdapterContract.from_operation(operation)
    request = ReadAdapterRequest(
        attempt_id="execution-attempt.unit.1",
        run_id="run.unit.read",
        step_id="step.unit.read",
        operation_key="operation.unit.read",
        policy_hash="a" * 64,
        attempt_number=1,
        call_deadline_at=NOW,
        correlation_id="correlation.unit.read",
        requested_timeout_seconds=10,
        provenance_ids=("input.unit.read",),
        input_classification=DataClassification.INTERNAL,
        contract=contract,
        input_payload=input_payload,
    )
    result = ReadAdapterResult.from_request(
        request,
        observation_id="observation.unit.read",
        output_payload={"items": [{"id": "one"}]},
        model_output_tokens=2,
    )
    input_payload["nested"] = {"values": [99]}

    assert request.input_payload["nested"] == {"values": (1, 2)}
    assert result.output_payload["items"] == ({"id": "one"},)
    assert request.request_schema_id == "schema.unit.read.request"
    assert request.result_schema_id == "schema.unit.read.result"
    assert result.attempt_id == request.attempt_id
    assert result.contract == request.contract
    assert result.provenance_ids == request.provenance_ids
    assert result.classification is DataClassification.INTERNAL
    assert result.trust_class == "untrusted_tool_result"
    assert (
        contract.max_input_bytes,
        contract.max_input_field_bytes,
        contract.max_output_bytes,
        contract.max_model_output_tokens,
    ) == (65_536, 16_384, 262_144, 4_096)
    with pytest.raises(TypeError):
        request.input_payload["new"] = True  # type: ignore[index]
    with pytest.raises(ValueError, match="nonempty immutable tuple"):
        replace(result, provenance_ids=())
    with pytest.raises(ValueError, match="classification differs"):
        replace(result, classification=DataClassification.PERSONAL)
    with pytest.raises(ValueError, match="nonnegative output token usage"):
        replace(result, model_output_tokens=None)

    tool_operation = replace(
        operation,
        kind=AttemptKind.TOOL,
        connector_family="connector",
        binding_id="binding.unit.read",
        binding_configuration_revision=1,
        request_redaction_fields=("/secret",),
        result_redaction_fields=("/secret",),
        data_classification=DataClassification.PERSONAL,
        connector_timeout_seconds=10,
    )
    tool_contract = ReadAdapterContract.from_operation(tool_operation)
    tool_request = replace(
        request,
        contract=tool_contract,
        input_classification=DataClassification.PERSONAL,
    )
    with pytest.raises(ValueError, match="cannot claim model output token usage"):
        ReadAdapterResult.from_request(
            tool_request,
            observation_id="observation.unit.tool",
            output_payload={"items": []},
            model_output_tokens=1,
        )


def test_orch_06_executor_has_no_write_dispatch_or_infrastructure_imports() -> None:
    source_path = (
        ROOT / "apps/api/src/marketing_agents/application/services/controlled_read_executor.py"
    )
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imports = {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    imports.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )

    assert not any("external_action_dispatcher" in name for name in imports)
    assert not any("external_writes" in name for name in imports)
    assert not any("infrastructure" in name for name in imports)

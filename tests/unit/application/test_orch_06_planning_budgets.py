"""ORCH-06: planning snapshots every effective runtime bound before authority."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

import pytest
from marketing_agents.application.orchestration import (
    EffectAwarePlanner,
    EffectPlanningError,
    EffectPlanRequest,
    EffectStepSpec,
    RoutingResult,
    SelectedInstanceSnapshot,
)
from marketing_agents.domain.data_classification import DataClassification
from marketing_agents.domain.enums import Effect
from marketing_agents.domain.graph import DependencyGraph, TopologyStep
from marketing_agents.domain.runtime_policy import (
    AttemptKind,
    BudgetPolicySnapshot,
    RateLimitPolicySnapshot,
    RateLimitScope,
    RetryBackoff,
    RetryPolicySnapshot,
    RunRuntimePolicy,
    TimeoutPolicySnapshot,
    runtime_operation_key,
    runtime_rate_limit_key,
)
from pydantic import BaseModel

CATALOG_HASH = "catalog-sha256-v1:" + "a" * 64
WORKFLOW_HASH = "b" * 64
ROUTING_HASH = "c" * 64
TEMPLATE_ID = "template.runtime"
INSTANCE_ID = "instance.runtime"
BINDING_ID = "binding.runtime.social"


@dataclass(frozen=True, slots=True)
class _Capability:
    id: str
    connector_family: str
    effect: str = "read"
    idempotency_support: str = "not_applicable"


@dataclass(frozen=True, slots=True)
class _Template:
    id: str
    allowed_tool_capability_ids: tuple[str, ...]
    operation_classification: str
    approval_policy_id: str
    input_schema_id: str
    output_schema_id: str
    retry_policy: RetryPolicySnapshot
    timeout_policy: TimeoutPolicySnapshot
    budget_policy: BudgetPolicySnapshot
    rate_limit_policy: RateLimitPolicySnapshot


@dataclass(frozen=True, slots=True)
class _ApprovalPolicy:
    id: str = "approval.none"
    kind: str = "none"
    required_roles: tuple[str, ...] = ()
    expiry_seconds: int = 60
    allow_self_approval: bool = False


class _ToolRequest(BaseModel):
    query: str


class _ToolResult(BaseModel):
    items: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _OperationMetadata:
    capability_id: str = "cap.social.read"
    connector_family: str = "social"
    effect: Effect = Effect.READ
    request_schema_id: str = "schema:connector:social.read:request:v1"
    result_schema_id: str = "schema:connector:social.read:result:v1"
    data_classification: DataClassification = DataClassification.PERSONAL
    idempotency_support: str = "not_applicable"
    default_timeout_seconds: int = 45
    request_redaction_fields: tuple[str, ...] = ()
    result_redaction_fields: tuple[str, ...] = ("/items",)
    enabled: bool = True
    disabled_reason: str | None = None


@dataclass(frozen=True, slots=True)
class _Operation:
    metadata: _OperationMetadata = _OperationMetadata()
    request_type: type[BaseModel] = _ToolRequest
    result_type: type[BaseModel] = _ToolResult


@dataclass(frozen=True, slots=True)
class _Binding:
    instance_id: str = INSTANCE_ID
    connector_family: str = "social"
    binding_id: str = BINDING_ID
    enabled: bool = True
    configuration_revision: int = 1


class _UnexpectedClock:
    def now(self) -> datetime:
        raise AssertionError("read-only planning must not consult the clock")


class _UnexpectedIds:
    def new(self, namespace: str) -> str:
        raise AssertionError(f"read-only planning must not allocate {namespace}")


def _template(
    *,
    retry: RetryPolicySnapshot | None = None,
    timeout: TimeoutPolicySnapshot | None = None,
    budget: BudgetPolicySnapshot | None = None,
    rate: RateLimitPolicySnapshot | None = None,
) -> _Template:
    return _Template(
        id=TEMPLATE_ID,
        allowed_tool_capability_ids=(
            "cap.model.generate",
            "cap.social.read",
            "cap.artifact.load",
        ),
        operation_classification="read_only",
        approval_policy_id="approval.none",
        input_schema_id="schema:template:runtime:input:v1",
        output_schema_id="schema:template:runtime:output:v1",
        retry_policy=retry or RetryPolicySnapshot(2, RetryBackoff.BOUNDED_EXPONENTIAL),
        timeout_policy=timeout or TimeoutPolicySnapshot(30, 120),
        budget_policy=budget or BudgetPolicySnapshot(5, 2, 2),
        rate_limit_policy=rate
        or RateLimitPolicySnapshot(RateLimitScope.TEMPLATE, TEMPLATE_ID, 20, 60),
    )


def _run_policy(**updates: int) -> RunRuntimePolicy:
    values = {
        "max_steps": 5,
        "max_model_calls": 2,
        "max_tool_calls": 2,
        "run_timeout_seconds": 300,
    }
    values.update(updates)
    return RunRuntimePolicy(**values)


def _planner(
    *,
    template: object | None = None,
    operation: object | None = None,
    run_policy: RunRuntimePolicy | None = None,
) -> EffectAwarePlanner:
    return EffectAwarePlanner(
        catalog_content_hash=CATALOG_HASH,
        clock=_UnexpectedClock(),
        ids=_UnexpectedIds(),
        capabilities=(
            _Capability("cap.model.generate", "model"),
            _Capability("cap.social.read", "social"),
            _Capability("cap.artifact.load", "artifact"),
        ),
        templates=(template or _template(),),  # type: ignore[arg-type]
        template_output_schemas={TEMPLATE_ID: {"type": "object"}},
        approval_policies=(_ApprovalPolicy(),),
        operations=(operation or _Operation(),),  # type: ignore[arg-type]
        bindings=(_Binding(),),
        run_policy=run_policy or _run_policy(),
    )


def _request(
    families: tuple[str, ...] = ("model", "social", "artifact"),
    *,
    workflow_version: int = 1,
) -> EffectPlanRequest:
    keys: list[str] = []
    specs: list[EffectStepSpec] = []
    topology: list[TopologyStep] = []
    capability_by_family = {
        "model": "cap.model.generate",
        "social": "cap.social.read",
        "artifact": "cap.artifact.load",
    }
    occurrences: dict[str, int] = {}
    for ordinal, family in enumerate(families, start=1):
        occurrences[family] = occurrences.get(family, 0) + 1
        key = f"{family}-{occurrences[family]}"
        keys.append(key)
        topology.append(
            TopologyStep(
                key=key,
                source_order=ordinal,
                dependency_keys=(() if ordinal == 1 else (keys[-2],)),
                terminal_result=ordinal == len(families),
            )
        )
        specs.append(
            EffectStepSpec(
                runtime_step_id=f"runtime-step.{key}",
                step_key=key,
                kind=f"{family}.read",
                selected_instance_id=INSTANCE_ID,
                routing_slot_key=None,
                capability_id=capability_by_family[family],
                binding_id=BINDING_ID if family == "social" else None,
            )
        )
    return EffectPlanRequest(
        run_id="run.runtime-policy",
        workflow_definition_hash=WORKFLOW_HASH,
        graph=DependencyGraph.build(tuple(topology), workflow_max_steps=20, global_max_steps=20),
        routing=RoutingResult(
            workflow_id="workflow.runtime-policy",
            workflow_version=workflow_version,
            catalog_content_hash=CATALOG_HASH,
            target_instance_id=INSTANCE_ID,
            selected_instances=(SelectedInstanceSnapshot(INSTANCE_ID, TEMPLATE_ID, 1, 1, 1),),
            assignments=(),
            semantic_hash=ROUTING_HASH,
        ),
        steps=tuple(specs),
        requested_by="principal.runtime-policy",
    )


def test_orch_06_planner_snapshots_exact_policy_and_derives_effective_run_timeout() -> None:
    plan = _planner().plan(_request())

    assert plan.run_policy == RunRuntimePolicy(
        max_steps=5,
        max_model_calls=2,
        max_tool_calls=2,
        run_timeout_seconds=120,
    )
    assert len(plan.run_policy.semantic_hash) == 64
    by_family = {step.connector_family: step for step in plan.steps}
    assert by_family["model"].runtime_policy.attempt_kind is AttemptKind.MODEL
    assert by_family["social"].runtime_policy.attempt_kind is AttemptKind.TOOL
    assert by_family["artifact"].runtime_policy.attempt_kind is AttemptKind.NO_CALL
    for step in plan.steps:
        policy = step.runtime_policy
        assert policy.operation_key == runtime_operation_key(
            workflow_id=plan.workflow_id,
            workflow_version=plan.workflow_version,
            step_key=step.step_key,
        )
        assert policy.retry == RetryPolicySnapshot(2, RetryBackoff.BOUNDED_EXPONENTIAL)
        assert policy.timeout == TimeoutPolicySnapshot(30, 120)
        assert policy.budget == BudgetPolicySnapshot(5, 2, 2)
        assert policy.rate_limit == RateLimitPolicySnapshot(
            RateLimitScope.TEMPLATE,
            runtime_rate_limit_key(
                template_id=TEMPLATE_ID,
                max_calls=20,
                window_seconds=60,
            ),
            20,
            60,
        )
        assert len(policy.semantic_hash) == 64
    assert by_family["social"].connector_timeout_seconds == 45
    assert by_family["social"].runtime_policy.timeout.step_seconds == 30
    assert by_family["social"].request_schema_id is not None
    assert by_family["social"].request_schema_id.endswith(":request:v1")
    assert by_family["social"].result_schema_id is not None
    assert by_family["social"].result_schema_id.endswith(":result:v1")
    assert by_family["social"].result_redaction_fields == ("/items",)
    assert by_family["social"].data_classification is DataClassification.PERSONAL
    assert by_family["model"].connector_timeout_seconds is None
    assert by_family["model"].request_schema_id == "schema:template:runtime:input:v1"
    assert by_family["model"].result_schema_id == "schema:template:runtime:output:v1"
    assert by_family["model"].data_classification is DataClassification.INTERNAL
    assert by_family["artifact"].connector_timeout_seconds is None
    assert by_family["artifact"].request_schema_id is None
    assert by_family["artifact"].result_schema_id is None


def test_orch_06_retry_authority_does_not_multiply_logical_planning_budget() -> None:
    template = _template(
        retry=RetryPolicySnapshot(3, RetryBackoff.BOUNDED_EXPONENTIAL),
        budget=BudgetPolicySnapshot(2, 1, 0),
    )

    plan = _planner(template=template, run_policy=_run_policy(max_model_calls=1)).plan(
        _request(("model", "artifact"))
    )

    assert plan.steps[0].runtime_policy.retry.max_attempts == 3
    assert plan.run_policy.max_model_calls == 1


@pytest.mark.parametrize(
    ("families", "template_budget", "run_policy", "code"),
    [
        (
            ("artifact", "artifact"),
            BudgetPolicySnapshot(1, 0, 0),
            _run_policy(),
            "template_step_budget_exceeded",
        ),
        (
            ("model", "model"),
            BudgetPolicySnapshot(3, 1, 0),
            _run_policy(),
            "template_model_budget_exceeded",
        ),
        (
            ("social", "social"),
            BudgetPolicySnapshot(3, 0, 1),
            _run_policy(),
            "template_tool_budget_exceeded",
        ),
        (
            ("artifact", "artifact"),
            BudgetPolicySnapshot(3, 0, 0),
            _run_policy(max_steps=1),
            "run_step_budget_exceeded",
        ),
        (
            ("model", "model"),
            BudgetPolicySnapshot(3, 2, 0),
            _run_policy(max_model_calls=1),
            "run_model_budget_exceeded",
        ),
        (
            ("social", "social"),
            BudgetPolicySnapshot(3, 0, 2),
            _run_policy(max_tool_calls=1),
            "run_tool_budget_exceeded",
        ),
    ],
)
def test_orch_06_template_and_run_limit_plus_one_fail_before_execution_authority(
    families: tuple[str, ...],
    template_budget: BudgetPolicySnapshot,
    run_policy: RunRuntimePolicy,
    code: str,
) -> None:
    planner = _planner(template=_template(budget=template_budget), run_policy=run_policy)

    with pytest.raises(EffectPlanningError) as captured:
        planner.plan(_request(families))

    assert captured.value.code == code


@pytest.mark.parametrize(
    "template",
    [
        replace(_template(), retry_policy=RetryPolicySnapshot(1, RetryBackoff.NONE)),
        replace(_template(), timeout_policy=TimeoutPolicySnapshot(29, 120)),
        replace(_template(), timeout_policy=TimeoutPolicySnapshot(30, 119)),
        replace(_template(), budget_policy=BudgetPolicySnapshot(5, 3, 2)),
        replace(_template(), budget_policy=BudgetPolicySnapshot(5, 2, 3)),
        replace(
            _template(),
            budget_policy=replace(_template().budget_policy, max_input_bytes=65_535),
        ),
        replace(
            _template(),
            budget_policy=replace(_template().budget_policy, max_input_field_bytes=16_383),
        ),
        replace(
            _template(),
            budget_policy=replace(_template().budget_policy, max_output_bytes=262_143),
        ),
        replace(
            _template(),
            budget_policy=replace(_template().budget_policy, max_model_output_tokens=4_095),
        ),
        replace(
            _template(),
            rate_limit_policy=RateLimitPolicySnapshot(RateLimitScope.TEMPLATE, TEMPLATE_ID, 19, 60),
        ),
        replace(
            _template(),
            rate_limit_policy=RateLimitPolicySnapshot(RateLimitScope.TEMPLATE, TEMPLATE_ID, 20, 61),
        ),
    ],
)
def test_orch_06_every_template_policy_change_changes_the_structural_plan_hash(
    template: _Template,
) -> None:
    request = _request()
    baseline = _planner().plan(request)
    changed = _planner(template=template).plan(request)

    assert changed.plan_hash != baseline.plan_hash


@pytest.mark.parametrize(
    "run_policy",
    [
        _run_policy(max_steps=4),
        _run_policy(max_model_calls=3),
        _run_policy(max_tool_calls=3),
        _run_policy(run_timeout_seconds=90),
    ],
)
def test_orch_06_every_effective_run_policy_change_changes_the_plan_hash(
    run_policy: RunRuntimePolicy,
) -> None:
    request = _request()
    baseline = _planner().plan(request)
    changed = _planner(run_policy=run_policy).plan(request)

    assert changed.plan_hash != baseline.plan_hash


def test_orch_06_forged_policy_with_original_hash_is_rejected() -> None:
    plan = _planner().plan(_request())
    changed_step = replace(
        plan.steps[0],
        runtime_policy=replace(
            plan.steps[0].runtime_policy,
            retry=RetryPolicySnapshot(1, RetryBackoff.NONE),
        ),
    )

    with pytest.raises(ValueError, match="hash"):
        replace(plan, steps=(changed_step, *plan.steps[1:]))


def test_orch_06_every_adapter_contract_field_is_plan_hash_bound() -> None:
    plan = _planner().plan(_request())
    target_index = next(
        index for index, step in enumerate(plan.steps) if step.connector_family == "social"
    )
    target = plan.steps[target_index]
    for changed in (
        replace(target, request_schema_id="schema:connector:social.read:request:v2"),
        replace(target, result_schema_id="schema:connector:social.read:result:v2"),
        replace(target, request_redaction_fields=("/query",)),
        replace(target, result_redaction_fields=("/other",)),
        replace(target, data_classification=DataClassification.SENSITIVE),
    ):
        changed_steps = list(plan.steps)
        changed_steps[target_index] = changed
        with pytest.raises(ValueError, match="hash"):
            replace(plan, steps=tuple(changed_steps))


def test_orch_06_missing_template_policy_fails_during_trusted_snapshot() -> None:
    valid = _template()

    @dataclass(frozen=True, slots=True)
    class _MissingRatePolicy:
        id: str = valid.id
        allowed_tool_capability_ids: tuple[str, ...] = valid.allowed_tool_capability_ids
        operation_classification: str = valid.operation_classification
        approval_policy_id: str = valid.approval_policy_id
        input_schema_id: str = valid.input_schema_id
        output_schema_id: str = valid.output_schema_id
        retry_policy: RetryPolicySnapshot = valid.retry_policy
        timeout_policy: TimeoutPolicySnapshot = valid.timeout_policy
        budget_policy: BudgetPolicySnapshot = valid.budget_policy

    with pytest.raises(EffectPlanningError) as captured:
        _planner(template=_MissingRatePolicy())

    assert captured.value.code == "invalid_template_runtime_policy"


def test_orch_06_read_result_schema_requires_a_typed_registered_result() -> None:
    @dataclass(frozen=True, slots=True)
    class _UntypedResultOperation:
        metadata: _OperationMetadata = _OperationMetadata()
        request_type: type[BaseModel] = _ToolRequest
        result_type: type[dict[str, object]] = dict

    with pytest.raises(EffectPlanningError) as captured:
        _planner(operation=_UntypedResultOperation())

    assert captured.value.code == "invalid_operation"

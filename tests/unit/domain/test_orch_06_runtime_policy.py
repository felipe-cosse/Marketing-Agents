"""ORCH-06: immutable policy snapshots and deterministic planning ceilings."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from typing import cast

import pytest
from marketing_agents.domain.plan_hash import EFFECT_PLAN_HASH_DOMAIN
from marketing_agents.domain.runtime_policy import (
    AttemptKind,
    BudgetPolicySnapshot,
    RateLimitPolicySnapshot,
    RateLimitScope,
    RetryBackoff,
    RetryPolicySnapshot,
    RunRuntimePolicy,
    RuntimePlanningBudgetError,
    StepRuntimeDemand,
    StepRuntimePolicy,
    TimeoutPolicySnapshot,
    canonical_payload_size_bytes,
    payload_fields_within_byte_limit,
    run_policy_projection,
    runtime_operation_key,
    runtime_rate_limit_key,
    step_policy_projection,
    validate_runtime_plan_budget,
)


def _policy(
    *,
    template_id: str = "template.alpha",
    step_key: str = "model",
    attempt_kind: AttemptKind = AttemptKind.MODEL,
    max_attempts: int = 3,
    max_steps: int = 3,
    max_model_calls: int = 2,
    max_tool_calls: int = 2,
) -> StepRuntimePolicy:
    return StepRuntimePolicy(
        operation_key=runtime_operation_key(
            workflow_id="workflow.policy",
            workflow_version=2,
            step_key=step_key,
        ),
        attempt_kind=attempt_kind,
        retry=RetryPolicySnapshot(max_attempts, RetryBackoff.BOUNDED_EXPONENTIAL),
        timeout=TimeoutPolicySnapshot(step_seconds=30, run_seconds=120),
        budget=BudgetPolicySnapshot(
            max_steps=max_steps,
            max_model_calls=max_model_calls,
            max_tool_calls=max_tool_calls,
        ),
        rate_limit=RateLimitPolicySnapshot(
            scope=RateLimitScope.TEMPLATE,
            key=template_id,
            max_calls=20,
            window_seconds=60,
        ),
    )


def _run_policy(**updates: int) -> RunRuntimePolicy:
    values = {
        "max_steps": 5,
        "max_model_calls": 3,
        "max_tool_calls": 3,
        "run_timeout_seconds": 300,
    }
    values.update(updates)
    return RunRuntimePolicy(**values)


def test_orch_06_policy_projections_are_frozen_canonical_and_complete() -> None:
    policy = _policy()
    run_policy = _run_policy()

    assert step_policy_projection(policy) == {
        "operation_key": runtime_operation_key(
            workflow_id="workflow.policy", workflow_version=2, step_key="model"
        ),
        "attempt_kind": "model",
        "max_attempts": 3,
        "backoff": "bounded_exponential",
        "step_timeout_seconds": 30,
        "template_run_timeout_seconds": 120,
        "max_steps": 3,
        "max_model_calls": 2,
        "max_tool_calls": 2,
        "max_input_bytes": 65_536,
        "max_input_field_bytes": 16_384,
        "max_output_bytes": 262_144,
        "max_model_output_tokens": 4_096,
        "rate_limit_scope": "template",
        "rate_limit_key": "template.alpha",
        "rate_limit_max_calls": 20,
        "rate_limit_window_seconds": 60,
    }
    assert run_policy_projection(run_policy) == {
        "max_steps": 5,
        "max_model_calls": 3,
        "max_tool_calls": 3,
        "run_timeout_seconds": 300,
    }
    assert policy.semantic_hash == (
        "3d68e23fcd8505eb701a902a59d4b4d18c46f29cbcf81853d051a6634c87cea7"
    )
    assert run_policy.semantic_hash == (
        "79ea6fa3d0e0c62d504e2e4f68273432cdb40a988b1ba09c5a473944cab2097c"
    )
    assert runtime_operation_key(
        workflow_id="workflow.policy", workflow_version=2, step_key="model"
    ) == (
        "runtime-operation-sha256-v1:"
        "f7e818518c59440be7015d384a578131409066e096c96f1af1aed1cbe338d2a0"
    )
    assert EFFECT_PLAN_HASH_DOMAIN == b"marketing-agents:effect-plan:v5\x00"
    with pytest.raises(FrozenInstanceError):
        policy.retry.max_attempts = 1  # type: ignore[misc]


def test_orch_06_rate_limit_identity_versions_incompatible_template_windows() -> None:
    baseline = runtime_rate_limit_key(
        template_id="template.alpha",
        max_calls=20,
        window_seconds=60,
    )

    assert baseline == runtime_rate_limit_key(
        template_id="template.alpha",
        max_calls=20,
        window_seconds=60,
    )
    assert baseline != runtime_rate_limit_key(
        template_id="template.alpha",
        max_calls=21,
        window_seconds=60,
    )
    assert baseline != runtime_rate_limit_key(
        template_id="template.alpha",
        max_calls=20,
        window_seconds=61,
    )


def test_orch_06_canonical_payload_and_utf8_field_limits_have_exact_edges() -> None:
    assert canonical_payload_size_bytes({"value": "é"}) == len('{"value":"é"}'.encode())
    assert payload_fields_within_byte_limit({"nested": ["é" * 4]}, 8)
    assert not payload_fields_within_byte_limit({"nested": ["é" * 4 + "x"]}, 8)
    assert not payload_fields_within_byte_limit({"é" * 5: True}, 8)


def test_orch_06_every_policy_field_is_semantically_hash_bound() -> None:
    baseline = _policy()
    variants = (
        replace(
            baseline,
            operation_key=runtime_operation_key(
                workflow_id="workflow.policy", workflow_version=2, step_key="other"
            ),
        ),
        replace(baseline, attempt_kind=AttemptKind.TOOL),
        replace(baseline, retry=RetryPolicySnapshot(2, RetryBackoff.BOUNDED_EXPONENTIAL)),
        replace(baseline, retry=RetryPolicySnapshot(3, RetryBackoff.NONE)),
        replace(baseline, timeout=TimeoutPolicySnapshot(29, 120)),
        replace(baseline, timeout=TimeoutPolicySnapshot(30, 121)),
        replace(baseline, budget=BudgetPolicySnapshot(4, 2, 2)),
        replace(baseline, budget=BudgetPolicySnapshot(3, 3, 2)),
        replace(baseline, budget=BudgetPolicySnapshot(3, 2, 3)),
        replace(
            baseline,
            budget=replace(baseline.budget, max_input_bytes=65_535),
        ),
        replace(
            baseline,
            budget=replace(baseline.budget, max_input_field_bytes=16_383),
        ),
        replace(
            baseline,
            budget=replace(baseline.budget, max_output_bytes=262_143),
        ),
        replace(
            baseline,
            budget=replace(baseline.budget, max_model_output_tokens=4_095),
        ),
        replace(
            baseline,
            rate_limit=RateLimitPolicySnapshot(RateLimitScope.TEMPLATE, "template.beta", 20, 60),
        ),
        replace(
            baseline,
            rate_limit=RateLimitPolicySnapshot(RateLimitScope.TEMPLATE, "template.alpha", 21, 60),
        ),
        replace(
            baseline,
            rate_limit=RateLimitPolicySnapshot(RateLimitScope.TEMPLATE, "template.alpha", 20, 61),
        ),
    )
    assert len({baseline.semantic_hash, *(item.semantic_hash for item in variants)}) == (
        len(variants) + 1
    )

    run_policy = _run_policy()
    run_variants = (
        replace(run_policy, max_steps=4),
        replace(run_policy, max_model_calls=2),
        replace(run_policy, max_tool_calls=2),
        replace(run_policy, run_timeout_seconds=299),
    )
    assert (
        len({run_policy.semantic_hash, *(item.semantic_hash for item in run_variants)})
        == len(run_variants) + 1
    )


@pytest.mark.parametrize(
    "factory",
    [
        lambda: RetryPolicySnapshot(0, RetryBackoff.NONE),
        lambda: RetryPolicySnapshot(4, RetryBackoff.NONE),
        lambda: RetryPolicySnapshot(1, cast(RetryBackoff, "none")),
        lambda: TimeoutPolicySnapshot(0, 1),
        lambda: TimeoutPolicySnapshot(2, 1),
        lambda: BudgetPolicySnapshot(0, 0, 0),
        lambda: BudgetPolicySnapshot(1, -1, 0),
        lambda: BudgetPolicySnapshot(1, 0, 0, max_input_bytes=0),
        lambda: BudgetPolicySnapshot(1, 0, 0, max_input_field_bytes=65_537),
        lambda: BudgetPolicySnapshot(1, 0, 0, max_output_bytes=4_194_305),
        lambda: BudgetPolicySnapshot(1, 0, 0, max_model_output_tokens=32_769),
        lambda: RateLimitPolicySnapshot(RateLimitScope.TEMPLATE, "template.alpha", 0, 1),
        lambda: RateLimitPolicySnapshot(RateLimitScope.TEMPLATE, "bad key", 1, 1),
        lambda: RunRuntimePolicy(True, 1, 1, 1),
        lambda: RunRuntimePolicy(1, 101, 1, 1),
        lambda: RunRuntimePolicy(1, 1, 1_001, 1),
        lambda: RunRuntimePolicy(1, 1, 1, 3_601),
    ],
)
def test_orch_06_policy_bounds_and_exact_types_fail_closed(factory: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        factory()  # type: ignore[operator]


def test_orch_06_logical_calls_do_not_multiply_retry_authority_and_no_call_is_free() -> None:
    model = _policy(max_model_calls=1, max_tool_calls=0)
    no_call = _policy(
        step_key="artifact",
        attempt_kind=AttemptKind.NO_CALL,
        max_model_calls=1,
        max_tool_calls=0,
    )

    totals = validate_runtime_plan_budget(
        _run_policy(max_model_calls=1, max_tool_calls=0),
        (
            StepRuntimeDemand("template.alpha", "model", model),
            StepRuntimeDemand("template.alpha", "artifact", no_call),
        ),
    )

    assert totals.step_count == 2
    assert totals.model_calls == 1
    assert totals.tool_calls == 0
    assert model.retry.max_attempts == 3


@pytest.mark.parametrize(
    ("run_policy", "demands", "code"),
    [
        (
            _run_policy(max_steps=1),
            (
                StepRuntimeDemand("template.alpha", "model", _policy()),
                StepRuntimeDemand(
                    "template.alpha",
                    "artifact",
                    _policy(step_key="artifact", attempt_kind=AttemptKind.NO_CALL),
                ),
            ),
            "run_step_budget_exceeded",
        ),
        (
            _run_policy(max_model_calls=1),
            (
                StepRuntimeDemand("template.alpha", "model", _policy(step_key="one")),
                StepRuntimeDemand("template.alpha", "model", _policy(step_key="two")),
            ),
            "run_model_budget_exceeded",
        ),
        (
            _run_policy(max_tool_calls=0),
            (
                StepRuntimeDemand(
                    "template.alpha",
                    "social",
                    _policy(step_key="tool", attempt_kind=AttemptKind.TOOL),
                ),
            ),
            "run_tool_budget_exceeded",
        ),
        (
            _run_policy(),
            (
                StepRuntimeDemand(
                    "template.alpha", "model", _policy(max_model_calls=1, step_key="one")
                ),
                StepRuntimeDemand(
                    "template.alpha", "model", _policy(max_model_calls=1, step_key="two")
                ),
            ),
            "template_model_budget_exceeded",
        ),
    ],
)
def test_orch_06_planning_budget_limit_plus_one_fails_with_stable_code(
    run_policy: RunRuntimePolicy,
    demands: tuple[StepRuntimeDemand, ...],
    code: str,
) -> None:
    with pytest.raises(RuntimePlanningBudgetError) as captured:
        validate_runtime_plan_budget(run_policy, demands)
    assert captured.value.code == code

"""Domain-owned structural hash contract shared by planning and persistence."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.data_classification import DataClassification
from marketing_agents.domain.enums import Effect
from marketing_agents.domain.runtime_policy import (
    RunRuntimePolicy,
    StepRuntimePolicy,
    run_policy_projection,
    step_policy_projection,
)
from marketing_agents.domain.schema_hash import require_schema_hash

EFFECT_PLAN_HASH_DOMAIN = b"marketing-agents:effect-plan:v5\x00"


@dataclass(frozen=True, slots=True)
class EffectPlanStepHashMaterial:
    """The exact step fields bound by the RUN-02 structural plan hash."""

    step_key: str
    kind: str
    selected_instance_id: str
    routing_slot_key: str | None
    template_id: str
    configuration_revision: int
    capability_id: str
    effect: Effect
    connector_family: str
    binding_id: str | None
    binding_configuration_revision: int | None
    request_schema_id: str | None
    result_schema_id: str | None
    result_schema_hash: str | None
    request_redaction_fields: tuple[str, ...]
    result_redaction_fields: tuple[str, ...]
    data_classification: DataClassification
    idempotency_support: str
    connector_timeout_seconds: int | None
    approval_policy_id: str
    approval_required_roles: tuple[str, ...]
    approval_required_scopes: tuple[str, ...]
    approval_expires_after_seconds: int | None
    approval_allow_self_approval: bool | None
    runtime_policy: StepRuntimePolicy

    def __post_init__(self) -> None:
        for values, name in (
            (self.request_redaction_fields, "request redaction fields"),
            (self.result_redaction_fields, "result redaction fields"),
            (self.approval_required_roles, "approval roles"),
            (self.approval_required_scopes, "approval scopes"),
        ):
            if type(values) is not tuple or any(type(value) is not str for value in values):
                raise ValueError(f"{name} must be an immutable string tuple")
        if type(self.effect) is not Effect:
            raise ValueError("plan hash effect must use the exact Effect enum")
        if type(self.data_classification) is not DataClassification:
            raise ValueError(
                "plan hash data classification must use the exact DataClassification enum"
            )
        if type(self.runtime_policy) is not StepRuntimePolicy:
            raise ValueError("plan hash runtime policy must use the exact immutable snapshot")
        if self.result_schema_hash is not None:
            require_schema_hash(self.result_schema_hash, "plan hash result schema hash")
        if (self.result_schema_id is None) != (self.result_schema_hash is None):
            raise ValueError("plan hash result schema ID and hash must be present together")


def effect_plan_hash(
    *,
    workflow_id: str,
    workflow_version: int,
    workflow_definition_hash: str,
    catalog_content_hash: str,
    graph_hash: str,
    routing_hash: str,
    run_policy: RunRuntimePolicy,
    steps: tuple[EffectPlanStepHashMaterial, ...],
) -> str:
    """Hash the stable RUN-02 plan projection without runtime/random identities."""

    if (
        type(steps) is not tuple
        or not steps
        or any(type(step) is not EffectPlanStepHashMaterial for step in steps)
    ):
        raise ValueError("plan hash steps must use exact immutable hash material")
    if type(run_policy) is not RunRuntimePolicy:
        raise ValueError("plan hash run policy must use the exact immutable contract")
    projection = {
        "version": 5,
        "workflow_id": workflow_id,
        "workflow_version": workflow_version,
        "workflow_definition_hash": workflow_definition_hash,
        "catalog_content_hash": catalog_content_hash,
        "graph_hash": graph_hash,
        "routing_hash": routing_hash,
        "run_runtime_policy": {
            **run_policy_projection(run_policy),
            "semantic_hash": run_policy.semantic_hash,
        },
        "steps": [
            {
                "step_key": step.step_key,
                "kind": step.kind,
                "selected_instance_id": step.selected_instance_id,
                "routing_slot_key": step.routing_slot_key,
                "template_id": step.template_id,
                "configuration_revision": step.configuration_revision,
                "capability_id": step.capability_id,
                "effect": step.effect.value,
                "connector_family": step.connector_family,
                "binding_id": step.binding_id,
                "binding_configuration_revision": step.binding_configuration_revision,
                "request_schema_id": step.request_schema_id,
                "result_schema_id": step.result_schema_id,
                "result_schema_hash": step.result_schema_hash,
                "request_redaction_fields": list(step.request_redaction_fields),
                "result_redaction_fields": list(step.result_redaction_fields),
                "data_classification": step.data_classification.value,
                "idempotency_support": step.idempotency_support,
                "connector_timeout_seconds": step.connector_timeout_seconds,
                "approval_policy": {
                    "id": step.approval_policy_id,
                    "required_roles": list(step.approval_required_roles),
                    "required_scopes": list(step.approval_required_scopes),
                    "expires_after_seconds": step.approval_expires_after_seconds,
                    "allow_self_approval": step.approval_allow_self_approval,
                },
                "runtime_policy": {
                    **step_policy_projection(step.runtime_policy),
                    "semantic_hash": step.runtime_policy.semantic_hash,
                },
            }
            for step in steps
        ],
    }
    return hashlib.sha256(EFFECT_PLAN_HASH_DOMAIN + canonical_json_bytes(projection)).hexdigest()


__all__ = [
    "EFFECT_PLAN_HASH_DOMAIN",
    "EffectPlanStepHashMaterial",
    "effect_plan_hash",
]

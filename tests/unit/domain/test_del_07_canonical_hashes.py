"""DEL-07: finite canonical JSON and immutable action-hash validation boundaries."""

from dataclasses import fields, replace
from typing import Any

import pytest
from marketing_agents.domain.action_hash import CanonicalExternalAction, SemanticExternalAction
from marketing_agents.domain.action_idempotency import derive_external_action_idempotency_key
from marketing_agents.domain.canonical_json import CanonicalJsonError, canonical_json_bytes
from marketing_agents.domain.enums import Effect, StepState
from marketing_agents.domain.plan_hash import EffectPlanStepHashMaterial, effect_plan_hash
from marketing_agents.domain.schema_hash import canonical_schema_hash, require_schema_hash
from marketing_agents.security import approval_digest
from marketing_agents.security.digest_key import DigestKey

from tests.unit.domain.test_orch_06_runtime_policy import _run_policy
from tests.unit.domain.test_orch_09_audit_contracts import _step
from tests.unit.domain.test_run_07_exact_action_approval import _action


def test_del_07_finite_float_is_canonical_and_preserved() -> None:
    assert canonical_json_bytes({"ratio": 1.25}) == b'{"ratio":1.25}'


@pytest.mark.parametrize("value", [b"secret", bytearray(b"secret"), {1, 2}, object()])
def test_del_07_non_json_objects_fail_instead_of_coercing(value: Any) -> None:
    with pytest.raises(CanonicalJsonError, match="unsupported canonical JSON"):
        canonical_json_bytes(value)


@pytest.mark.parametrize("value", [True, 0, -1, 1.0, "1"])
def test_del_07_key_material_revision_requires_positive_integer(value: Any) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        replace(_action().key_material(), proposal_revision=value)


def test_del_07_semantic_and_exact_destinations_must_already_be_normalized() -> None:
    action = _action()
    semantic = action.semantic_action().semantic_projection()
    semantic["destination"] = " list:newsletter "
    with pytest.raises(ValueError, match="already be normalized"):
        SemanticExternalAction.model_validate(semantic)
    exact = action.authorization_projection()
    exact["destination"] = " list:newsletter "
    with pytest.raises(ValueError, match="already be normalized"):
        CanonicalExternalAction.model_validate(exact)


def test_del_07_exact_action_rejects_stale_semantic_hash_and_boolean_revision() -> None:
    values = _action().authorization_projection()
    with pytest.raises(ValueError, match="semantic action hash is not current"):
        CanonicalExternalAction.model_validate({**values, "semantic_action_hash": "0" * 64})
    with pytest.raises(ValueError, match="valid integer"):
        CanonicalExternalAction.model_validate({**values, "proposal_revision": True})


def test_del_07_revision_validator_helper_rejects_boolean_without_coercion() -> None:
    # The public strict model rejects bool first; the validator itself must also
    # retain its stated positive-integer contract when explicitly invoked.
    with pytest.raises(ValueError, match="positive integer"):
        CanonicalExternalAction.reject_boolean_revision(True)
    assert CanonicalExternalAction.reject_boolean_revision(1) == 1


def _plan_step() -> EffectPlanStepHashMaterial:
    step = _step(Effect.READ, StepState.PENDING)
    aliases = {"step_key": "key", "connector_timeout_seconds": "timeout_seconds"}
    return EffectPlanStepHashMaterial(
        **{
            field.name: getattr(step, aliases.get(field.name, field.name))
            for field in fields(EffectPlanStepHashMaterial)
        }
    )


def _plan_arguments() -> dict[str, Any]:
    return dict(
        workflow_id="workflow.test",
        workflow_version=1,
        workflow_definition_hash="a" * 64,
        catalog_content_hash="catalog-sha256-v1:" + "b" * 64,
        graph_hash="c" * 64,
        routing_hash="d" * 64,
        run_policy=_run_policy(),
        steps=(_plan_step(),),
    )


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"request_redaction_fields": []}, "immutable string tuple"),
        ({"approval_required_roles": (1,)}, "immutable string tuple"),
        ({"effect": "read"}, "Effect enum"),
        ({"data_classification": "internal"}, "DataClassification enum"),
        ({"runtime_policy": object()}, "exact immutable"),
        ({"result_schema_id": None}, "present together"),
        ({"result_schema_hash": None}, "present together"),
    ],
)
def test_del_07_plan_material_rejects_unsealed_policy_or_schema(
    changes: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        replace(_plan_step(), **changes)


@pytest.mark.parametrize(
    "changes", [{"steps": []}, {"steps": ()}, {"steps": (object(),)}, {"run_policy": object()}]
)
def test_del_07_plan_hash_requires_exact_immutable_input(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="exact immutable"):
        effect_plan_hash(**{**_plan_arguments(), **changes})


def test_del_07_plan_hash_changes_with_schema_policy_and_step_meaning() -> None:
    arguments = _plan_arguments()
    original = effect_plan_hash(**arguments)
    assert effect_plan_hash(**arguments) == original
    for changes in (
        {"step_key": "other"},
        {"result_schema_hash": "schema-sha256-v1:" + "f" * 64},
        {"request_redaction_fields": ("/body",)},
        {"result_schema_id": None, "result_schema_hash": None},
    ):
        assert (
            effect_plan_hash(**{**arguments, "steps": (replace(_plan_step(), **changes),)})
            != original
        )
    assert effect_plan_hash(**{**arguments, "run_policy": _run_policy(max_steps=6)}) != original


def test_del_07_schema_hash_is_order_independent_and_sensitive_to_content() -> None:
    first = canonical_schema_hash({"type": "string", "maxLength": 3})
    assert first == canonical_schema_hash({"maxLength": 3, "type": "string"})
    assert first != canonical_schema_hash({"type": "string", "maxLength": 4})
    require_schema_hash(first, "test schema")
    with pytest.raises(ValueError, match="object mapping"):
        canonical_schema_hash([])  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "value", [None, 1, "a" * 64, "schema-sha256-v1:" + "A" * 64, "schema-sha256-v2:" + "a" * 64]
)
def test_del_07_schema_identity_rejects_malformed_or_unknown_versions(value: Any) -> None:
    with pytest.raises(ValueError, match="canonical JSON Schema hash"):
        require_schema_hash(value, "test schema")


def test_del_07_approval_record_digests_are_keyed_and_domain_separated() -> None:
    functions = (
        approval_digest.approval_request_record_digest,
        approval_digest.approval_decision_record_digest,
        approval_digest.approval_use_record_digest,
        approval_digest.authorization_set_record_digest,
        approval_digest.authorization_set_head_record_digest,
        approval_digest.authorization_set_member_record_digest,
    )
    key = DigestKey(b"a" * 32)
    material = {"action": "synthetic", "version": 1}
    digests = [function(material, key) for function in functions]
    assert len(set(digests)) == len(functions)
    for function, digest in zip(functions, digests, strict=True):
        assert digest == function({"version": 1, "action": "synthetic"}, key)
        assert digest != function(material, DigestKey(b"b" * 32))
        assert digest != function({**material, "version": 2}, key)


def test_del_07_delivery_identity_is_stable_but_changes_for_a_new_proposal() -> None:
    material = _action().key_material()
    original = derive_external_action_idempotency_key(material)
    assert original == derive_external_action_idempotency_key(material)
    assert original.startswith("action-idempotency-v1:")
    assert original != derive_external_action_idempotency_key(
        replace(material, proposal_revision=2)
    )

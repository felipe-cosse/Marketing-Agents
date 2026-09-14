"""OBJ-03 planner-only proposals cannot masquerade as connector execution."""

from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from marketing_agents.application.services.audit_events import AuditEventFactory
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.data_classification import DataClassification
from marketing_agents.domain.entities import RunStep
from marketing_agents.domain.enums import Effect, StepState
from marketing_agents.domain.planner_output import (
    PLANNER_OUTPUT_FAMILY,
    PROPOSAL_PREVIEW_CAPABILITIES,
    PROPOSAL_PREVIEW_KIND,
)
from marketing_agents.domain.provenance import ArtifactEnvelope, ProviderVersion
from marketing_agents.domain.runtime_policy import AttemptKind, attempt_kind_for_connector

from tests.unit.application.test_api_07_artifact_resources import _artifact, _step

PROVIDER = ProviderVersion(
    provider_kind="planner", mode="local", name="catalog-write-proposal", version="v1"
)


def _preview_step() -> RunStep:
    original = _step()
    return replace(
        original,
        connector_family=PLANNER_OUTPUT_FAMILY,
        kind=PROPOSAL_PREVIEW_KIND,
        capability_id="cap.newsletter.subscribe",
        binding_id=None,
        binding_configuration_revision=None,
        timeout_seconds=None,
        result_redaction_fields=(),
        runtime_policy=replace(original.runtime_policy, attempt_kind=AttemptKind.NO_CALL),
        state=StepState.SUCCEEDED,
        terminal_reason_code="step_succeeded",
        version=3,
    )


def _preview_artifact(**changes: Any) -> ArtifactEnvelope:
    original = _artifact().artifact
    return original.model_copy(
        update={
            "provenance": original.provenance.model_copy(
                update={"providers": (PROVIDER,), **changes}
            )
        }
    )


def _factory() -> AuditEventFactory:
    return AuditEventFactory(
        AuditContext.worker("worker.obj03.preview", correlation_id="corr.obj03.preview")
    )


@pytest.mark.parametrize("capability_id", sorted(PROPOSAL_PREVIEW_CAPABILITIES))
def test_obj_03_preview_retains_original_write_capability_but_has_no_call_or_approval(
    capability_id: str,
) -> None:
    step = replace(_preview_step(), capability_id=capability_id)
    assert attempt_kind_for_connector(PLANNER_OUTPUT_FAMILY) is AttemptKind.NO_CALL
    assert step.effect is Effect.READ and step.runtime_policy.attempt_kind is AttemptKind.NO_CALL
    event = _factory().artifact_previewed(_preview_artifact(), step)
    event.verify_integrity()
    assert event.event_type == "artifact.previewed"
    assert event.aggregate_type == "artifact"
    assert event.attempt_id is event.action_id is event.approval_request_id is None
    assert event.artifact_id == _preview_artifact().provenance.artifact_id
    assert set(event.safe_metadata.values) == {
        "data_classification",
        "output_schema_id",
        "output_schema_hash",
        "output_schema_version",
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "connector.read"},
        {"connector_family": "artifact"},
        {"capability_id": "cap.artifact.transform-deterministic"},
        {"effect": Effect.WRITE},
        {"idempotency_support": "required"},
        {"request_schema_id": None},
        {"result_schema_id": None, "result_schema_hash": None},
        {"binding_id": "binding.preview.forbidden"},
        {"binding_configuration_revision": 1},
        {"timeout_seconds": 30},
        {"request_redaction_fields": ("/secret",)},
        {"result_redaction_fields": ("/secret",)},
        {"data_classification": DataClassification.PERSONAL},
        {"approval_required_roles": ("approver",)},
        {"approval_required_scopes": ("scope.external-write",)},
        {"approval_expires_after_seconds": 300},
        {"approval_allow_self_approval": False},
        {"state": StepState.AWAITING_APPROVAL, "terminal_reason_code": None},
    ],
)
def test_obj_03_preview_domain_rejects_connector_or_approval_authority(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        replace(_preview_step(), **changes)


def test_obj_03_reserved_preview_kind_cannot_label_a_real_write_or_call() -> None:
    with pytest.raises(ValueError, match="preview kind and family"):
        replace(
            _step(),
            kind=PROPOSAL_PREVIEW_KIND,
            effect=Effect.WRITE,
            idempotency_support="required",
            approval_required_roles=("approver",),
            approval_required_scopes=("scope.external-write",),
            approval_expires_after_seconds=300,
            approval_allow_self_approval=False,
        )
    for kind in (AttemptKind.MODEL, AttemptKind.TOOL):
        with pytest.raises(ValueError, match="runtime attempt kind"):
            replace(
                _preview_step(),
                runtime_policy=replace(_preview_step().runtime_policy, attempt_kind=kind),
            )


@pytest.mark.parametrize(
    "changes",
    [
        {"run_id": "run.different"},
        {"step_id": "step.different"},
        {"template_id": "template.different"},
        {"instance_id": "instance.different"},
        {"instance_config_revision": 2},
        {"output_schema_id": "schema.different"},
        {"output_schema_hash": "schema-sha256-v1:" + "0" * 64},
        {"created_at": _step().updated_at + timedelta(seconds=1)},
        {"providers": ()},
        {"providers": (PROVIDER, PROVIDER)},
        {"providers": (PROVIDER.model_copy(update={"name": "catalog-role-transform"}),)},
        {"providers": (PROVIDER.model_copy(update={"version": "v2"}),)},
        {"providers": (PROVIDER.model_copy(update={"mode": "mock"}),)},
        {"providers": (PROVIDER.model_copy(update={"provider_kind": "connector"}),)},
    ],
)
def test_obj_03_preview_audit_requires_exact_output_lineage_and_local_provider(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        _factory().artifact_previewed(_preview_artifact(**changes), _preview_step())


def test_obj_03_preview_audit_rejects_unfinished_or_transform_outputs() -> None:
    with pytest.raises(ValueError, match="succeeded planner output"):
        _factory().artifact_previewed(
            _preview_artifact(),
            replace(_preview_step(), state=StepState.READY, terminal_reason_code=None),
        )
    with pytest.raises(ValueError):
        _factory().artifact_transformed(_preview_artifact(), _preview_step())
    artifact = _preview_artifact()
    artifact.payload["tampered"] = True
    with pytest.raises(ValueError):
        _factory().artifact_previewed(artifact, _preview_step())

"""ORCH-02: one sealed, schema-bounded incoming-work validation boundary."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from marketing_agents.application.orchestration import OrchestrationDependencies
from marketing_agents.application.policies.runtime_guard import (
    CapabilityPolicy,
    RuntimePolicyGuard,
    RuntimePolicySnapshot,
)
from marketing_agents.application.ports.unit_of_work import UnitOfWork
from marketing_agents.application.services import (
    CampaignBriefPolicy,
    CampaignBriefRevision,
    ConfiguredIncomingTrigger,
    IncomingWorkValidationError,
    IncomingWorkValidator,
    ValidatedIncomingWork,
    WorkAdmissionService,
    WorkflowAdmissionDefinition,
)
from marketing_agents.domain.admission import AdmissionEnvelope
from marketing_agents.domain.enums import TriggerKind, WorkMode
from marketing_agents.infrastructure.catalog import compile_catalog
from marketing_agents.security.digest_key import DigestKey

ROOT = Path(__file__).resolve().parents[3]
CATALOG_HASH = "catalog-sha256-v1:" + ("c" * 64)
SCHEMA_ID = "urn:marketing-agents:test:orch-02:input"
TEMPLATE_ID = "tpl.test.orch-02.validator"
INSTANCE_ID = "inst.test.orch-02.validator.01"
NOW = datetime(2026, 8, 18, 12, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class TemplateStub:
    id: str = TEMPLATE_ID
    supported_trigger_types: tuple[str, ...] = ("manual",)


@dataclass(frozen=True, slots=True)
class InstanceStub:
    id: str = INSTANCE_ID
    template_id: str = TEMPLATE_ID
    enabled: bool = True
    configuration_revision: int = 3


def _guard(
    *,
    input_max_bytes: int = 50_000,
    max_json_depth: int = 12,
) -> RuntimePolicyGuard:
    policy = RuntimePolicySnapshot(
        allowed_capabilities=(
            CapabilityPolicy(
                capability_id="catalog.read",
                effect="read",
                connector_family="catalog",
            ),
        ),
        input_max_bytes=input_max_bytes,
        output_max_bytes=50_000,
        max_json_depth=max_json_depth,
        max_content_parts=10,
        max_content_characters=50_000,
        max_model_calls=2,
        max_tool_calls=2,
        rate_window_max_calls=10,
        rate_window_seconds=60,
        step_timeout_seconds=10,
        run_timeout_seconds=60,
    )
    return RuntimePolicyGuard(policy)


def _schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        "type": "object",
        "additionalProperties": False,
        "required": ["request_id", "source_content"],
        "properties": {
            "request_id": {"type": "string", "minLength": 1, "maxLength": 80},
            "source_content": {"type": "string", "minLength": 1, "maxLength": 12_000},
        },
    }


def _envelope(**updates: object) -> AdmissionEnvelope:
    values: dict[str, object] = {
        "source": "manual",
        "event_id": "event.orch-02.0001",
        "instance_id": INSTANCE_ID,
        "trigger_id": "trigger.orch-02.manual",
        "workflow_id": "workflow.orch-02.v1",
        "mode": WorkMode.MOCK_EXECUTION,
        "brief_id": None,
        "brief_revision": None,
        "configuration_revision": 3,
        "admitted_payload": {"request_id": "request-1", "source_content": "hello"},
    }
    values.update(updates)
    return AdmissionEnvelope(**values)  # type: ignore[arg-type]


def _trigger(**updates: object) -> ConfiguredIncomingTrigger:
    values: dict[str, object] = {
        "id": "trigger.orch-02.manual",
        "instance_id": INSTANCE_ID,
        "kind": TriggerKind.MANUAL,
        "source": "manual",
        "workflow_ids": ("workflow.orch-02.v1",),
        "enabled": True,
    }
    values.update(updates)
    return ConfiguredIncomingTrigger(**values)  # type: ignore[arg-type]


def _workflow(**updates: object) -> WorkflowAdmissionDefinition:
    values: dict[str, object] = {
        "id": "workflow.orch-02.v1",
        "eligible_template_ids": (TEMPLATE_ID,),
        "eligible_trigger_kinds": (TriggerKind.MANUAL,),
        "allowed_modes": (WorkMode.DRY_RUN, WorkMode.MOCK_EXECUTION),
        "input_schema_ids_by_template": {TEMPLATE_ID: SCHEMA_ID},
        "campaign_brief_policy": CampaignBriefPolicy.OPTIONAL,
        "enabled": True,
    }
    values.update(updates)
    return WorkflowAdmissionDefinition(**values)  # type: ignore[arg-type]


def _validator(
    *,
    catalog_hash: str = CATALOG_HASH,
    templates: tuple[TemplateStub, ...] = (TemplateStub(),),
    instances: tuple[InstanceStub, ...] = (InstanceStub(),),
    schema: Mapping[str, Any] | None = None,
    triggers: tuple[ConfiguredIncomingTrigger, ...] | None = None,
    workflows: tuple[WorkflowAdmissionDefinition, ...] | None = None,
    briefs: tuple[CampaignBriefRevision, ...] = (),
    guard: object | None = None,
) -> IncomingWorkValidator:
    return IncomingWorkValidator(
        catalog_hash=catalog_hash,
        templates=templates,
        instances=instances,
        input_schemas_by_template={TEMPLATE_ID: schema or _schema()},
        triggers=triggers if triggers is not None else (_trigger(),),
        workflows=workflows if workflows is not None else (_workflow(),),
        campaign_brief_revisions=briefs,
        guard=cast(Any, guard or _guard()),
    )


def test_orch_02_all_43_compiled_instances_validate_against_their_template_schema() -> None:
    catalog = compile_catalog(ROOT / "catalog" / "v1")
    workflows: list[WorkflowAdmissionDefinition] = []
    triggers: list[ConfiguredIncomingTrigger] = []
    bindings: dict[str, tuple[str, str]] = {}
    for index, instance in enumerate(catalog.instances, start=1):
        workflow_id = f"workflow.orch-02.{index:02d}"
        trigger_id = f"trigger.orch-02.{index:02d}"
        schema_id = cast(str, catalog.input_schema_by_template[instance.template_id]["$id"])
        workflows.append(
            WorkflowAdmissionDefinition(
                id=workflow_id,
                eligible_template_ids=(instance.template_id,),
                eligible_trigger_kinds=(TriggerKind.MANUAL,),
                allowed_modes=(WorkMode.DRY_RUN, WorkMode.MOCK_EXECUTION),
                input_schema_ids_by_template={instance.template_id: schema_id},
            )
        )
        triggers.append(
            ConfiguredIncomingTrigger(
                id=trigger_id,
                instance_id=instance.id,
                kind=TriggerKind.MANUAL,
                source="manual",
                workflow_ids=(workflow_id,),
            )
        )
        bindings[instance.id] = (trigger_id, workflow_id)
    validator = IncomingWorkValidator(
        catalog_hash=catalog.content_hash,
        templates=catalog.templates,
        instances=catalog.instances,
        input_schemas_by_template=catalog.input_schema_by_template,
        triggers=tuple(triggers),
        workflows=tuple(workflows),
        campaign_brief_revisions=(),
        guard=_guard(),
    )

    validated_ids = set()
    for index, instance in enumerate(catalog.instances, start=1):
        trigger_id, workflow_id = bindings[instance.id]
        validated = validator.validate(
            AdmissionEnvelope(
                source="manual",
                event_id=f"event.orch-02.{index:02d}",
                instance_id=instance.id,
                trigger_id=trigger_id,
                workflow_id=workflow_id,
                mode=WorkMode.DRY_RUN,
                brief_id=None,
                brief_revision=None,
                configuration_revision=instance.configuration_revision,
                admitted_payload={
                    "request_id": f"request-{index}",
                    "source_content": "bounded incoming content",
                },
            )
        )
        validated_ids.add(validated.snapshot.instance_id)
        assert validated.snapshot.catalog_hash == catalog.content_hash
        assert validated.snapshot.template_id == instance.template_id
        assert validated.snapshot.input_schema_hash.startswith("schema-sha256-v1:")

    assert validated_ids == {item.id for item in catalog.instances}
    assert len(validated_ids) == 43


@pytest.mark.parametrize(
    ("validator", "envelope", "code"),
    [
        (_validator(), _envelope(instance_id="inst.unknown.01"), "instance_unknown"),
        (
            _validator(instances=(replace(InstanceStub(), enabled=False),)),
            _envelope(),
            "instance_disabled",
        ),
        (_validator(templates=()), _envelope(), "template_unknown"),
        (_validator(), _envelope(configuration_revision=4), "instance_configuration_mismatch"),
        (_validator(workflows=()), _envelope(), "workflow_unknown"),
        (
            _validator(workflows=(replace(_workflow(), enabled=False),)),
            _envelope(),
            "workflow_disabled",
        ),
        (
            _validator(
                workflows=(
                    _workflow(
                        eligible_template_ids=("tpl.other",),
                        input_schema_ids_by_template={"tpl.other": SCHEMA_ID},
                    ),
                )
            ),
            _envelope(),
            "workflow_template_mismatch",
        ),
        (
            _validator(workflows=(_workflow(allowed_modes=(WorkMode.DRY_RUN,)),)),
            _envelope(),
            "work_mode_not_allowed",
        ),
        (_validator(triggers=()), _envelope(), "trigger_unknown"),
        (
            _validator(triggers=(replace(_trigger(), enabled=False),)),
            _envelope(),
            "trigger_disabled",
        ),
        (
            _validator(triggers=(_trigger(source="webhook"),)),
            _envelope(),
            "trigger_source_mismatch",
        ),
        (
            _validator(triggers=(_trigger(instance_id="inst.other.01"),)),
            _envelope(),
            "trigger_instance_mismatch",
        ),
        (
            _validator(triggers=(_trigger(workflow_ids=("workflow.other",)),)),
            _envelope(),
            "trigger_workflow_mismatch",
        ),
        (
            _validator(templates=(TemplateStub(supported_trigger_types=("webhook",)),)),
            _envelope(),
            "template_trigger_unsupported",
        ),
        (
            _validator(workflows=(_workflow(eligible_trigger_kinds=(TriggerKind.WEBHOOK,)),)),
            _envelope(),
            "workflow_trigger_mismatch",
        ),
        (
            _validator(
                workflows=(
                    _workflow(input_schema_ids_by_template={TEMPLATE_ID: "urn:schema:other"}),
                )
            ),
            _envelope(),
            "workflow_schema_mismatch",
        ),
        (
            _validator(workflows=(_workflow(campaign_brief_policy=CampaignBriefPolicy.REQUIRED),)),
            _envelope(),
            "campaign_brief_required",
        ),
        (
            _validator(),
            _envelope(brief_id="brief.unknown", brief_revision=1),
            "campaign_brief_unknown",
        ),
        (
            _validator(
                workflows=(_workflow(campaign_brief_policy=CampaignBriefPolicy.FORBIDDEN),),
                briefs=(CampaignBriefRevision("brief.orch-02", 1),),
            ),
            _envelope(brief_id="brief.orch-02", brief_revision=1),
            "campaign_brief_forbidden",
        ),
        (
            _validator(briefs=(CampaignBriefRevision("brief.orch-02", 1, enabled=False),)),
            _envelope(brief_id="brief.orch-02", brief_revision=1),
            "campaign_brief_disabled",
        ),
        (
            _validator(),
            _envelope(admitted_payload={"request_id": "missing-content"}),
            "input_schema_invalid",
        ),
    ],
)
def test_orch_02_unknown_disabled_mismatched_and_schema_invalid_inputs_fail_closed(
    validator: IncomingWorkValidator,
    envelope: AdmissionEnvelope,
    code: str,
) -> None:
    with pytest.raises(IncomingWorkValidationError) as rejected:
        validator.validate(envelope)
    assert rejected.value.code == code
    assert "source_content" not in repr(rejected.value)


def test_orch_02_exact_campaign_brief_revision_is_validated() -> None:
    brief = CampaignBriefRevision("brief.orch-02", 2)
    validated = _validator(briefs=(brief,)).validate(
        _envelope(brief_id=brief.id, brief_revision=brief.revision)
    )
    assert validated.envelope.brief_id == brief.id
    assert validated.envelope.brief_revision == 2


def test_orch_02_malformed_compiled_input_schema_has_stable_rejection() -> None:
    malformed = _schema()
    malformed["type"] = "not-a-json-schema-type"
    with pytest.raises(IncomingWorkValidationError) as rejected:
        _validator(schema=malformed).validate(_envelope())
    assert rejected.value.code == "input_schema_invalid"


@pytest.mark.parametrize(
    ("validator", "envelope", "code"),
    [
        (
            _validator(guard=_guard(input_max_bytes=20)),
            _envelope(),
            "input_byte_limit",
        ),
        (
            _validator(
                schema={"$id": SCHEMA_ID, "type": "object"},
                guard=_guard(max_json_depth=3),
            ),
            _envelope(admitted_payload={"a": {"b": {"c": "too-deep"}}}),
            "json_depth_limit",
        ),
        (
            _validator(),
            _envelope(
                admitted_payload={
                    "request_id": "request-1",
                    "source_content": "hello",
                    "unexpected": "closed schema",
                }
            ),
            "input_schema_invalid",
        ),
    ],
)
def test_orch_02_central_guard_enforces_bytes_depth_and_closed_schema(
    validator: IncomingWorkValidator,
    envelope: AdmissionEnvelope,
    code: str,
) -> None:
    with pytest.raises(IncomingWorkValidationError) as rejected:
        validator.validate(envelope)
    assert rejected.value.code == code


class PlainPayloadGuard:
    def __init__(self) -> None:
        self.payload: object | None = None

    def validate_input(self, payload: Any, schema: Mapping[str, Any]) -> None:
        del schema
        self.payload = payload


def test_orch_02_thaws_nested_frozen_payload_before_safe_06_guard() -> None:
    schema = {
        "$id": SCHEMA_ID,
        "type": "object",
        "properties": {"data": {"type": "object"}},
    }
    workflow = _workflow()
    spy = PlainPayloadGuard()
    envelope = _envelope(admitted_payload={"data": {"items": ["one", "two"]}})
    assert not isinstance(envelope.admitted_payload, dict)
    assert isinstance(envelope.admitted_payload["data"]["items"], tuple)

    _validator(schema=schema, workflows=(workflow,), guard=spy).validate(envelope)

    assert isinstance(spy.payload, dict)
    assert isinstance(cast(dict[str, Any], spy.payload)["data"], dict)
    assert isinstance(cast(dict[str, Any], spy.payload)["data"]["items"], list)


def test_orch_02_worker_revalidation_detects_same_id_schema_content_drift() -> None:
    original_validator = _validator()
    admitted = original_validator.validate(_envelope())
    unchanged = original_validator.validate(
        admitted.envelope,
        expected_snapshot=admitted.snapshot,
    )
    assert unchanged.snapshot == admitted.snapshot

    changed_schema = _schema()
    changed_schema["title"] = "Same ID, changed semantic content"
    changed_validator = _validator(
        catalog_hash="catalog-sha256-v1:" + ("d" * 64),
        schema=changed_schema,
    )
    with pytest.raises(IncomingWorkValidationError) as drift:
        changed_validator.validate(
            admitted.envelope,
            expected_snapshot=admitted.snapshot,
        )
    assert drift.value.code == "input_schema_drift"

    catalog_only_validator = _validator(
        catalog_hash="catalog-sha256-v1:" + ("e" * 64),
    )
    with pytest.raises(IncomingWorkValidationError) as catalog_drift:
        catalog_only_validator.validate(
            admitted.envelope,
            expected_snapshot=admitted.snapshot,
        )
    assert catalog_drift.value.code == "catalog_drift"


def test_orch_02_validator_snapshots_schema_content_at_construction() -> None:
    caller_owned_schema = _schema()
    validator = _validator(schema=caller_owned_schema)
    admitted = validator.validate(_envelope())

    caller_owned_schema["title"] = "external mutation after validator construction"
    revalidated = validator.validate(
        admitted.envelope,
        expected_snapshot=admitted.snapshot,
    )

    assert revalidated.snapshot == admitted.snapshot


def test_orch_02_private_admission_helper_has_no_non_service_production_callers() -> None:
    source_root = ROOT / "apps" / "api" / "src" / "marketing_agents"
    owner = source_root / "application" / "services" / "work_admission.py"
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in source_root.rglob("*.py")
        if path != owner and "_admit_envelope_in_uow" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


class ProbeClock:
    def __init__(self) -> None:
        self.calls = 0

    def now(self) -> datetime:
        self.calls += 1
        return NOW


class ProbeIds:
    def __init__(self) -> None:
        self.calls = 0

    def new(self, namespace: str) -> str:
        self.calls += 1
        return f"{namespace}.unexpected"


class ProbeUnitOfWorkFactory:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> UnitOfWork:
        self.calls += 1
        raise AssertionError("unit of work must not be created for unvalidated input")


@pytest.mark.asyncio
async def test_orch_02_public_admission_rejects_raw_or_fabricated_marker_before_side_effects() -> (
    None
):
    clock = ProbeClock()
    ids = ProbeIds()
    unit_of_work_factory = ProbeUnitOfWorkFactory()
    service = WorkAdmissionService(
        OrchestrationDependencies(clock, ids, unit_of_work_factory),
        DigestKey(bytes(range(32))),
    )
    raw = _envelope()

    with pytest.raises(TypeError, match="only be issued"):
        ValidatedIncomingWork()
    with pytest.raises(IncomingWorkValidationError) as raw_rejected:
        await service.admit(cast(ValidatedIncomingWork, raw))
    assert raw_rejected.value.code == "incoming_work_not_validated"

    valid = _validator().validate(raw)
    fabricated = object.__new__(ValidatedIncomingWork)
    object.__setattr__(fabricated, "envelope", valid.envelope)
    object.__setattr__(fabricated, "snapshot", valid.snapshot)
    object.__setattr__(fabricated, "_seal", object())
    with pytest.raises(IncomingWorkValidationError) as fabricated_rejected:
        await service.admit(fabricated)
    assert fabricated_rejected.value.code == "incoming_work_not_validated"

    with pytest.raises(IncomingWorkValidationError):
        await service.admit_in_uow(cast(UnitOfWork, object()), cast(ValidatedIncomingWork, raw))
    assert unit_of_work_factory.calls == 0
    assert ids.calls == 0
    assert clock.calls == 0

"""Pure fail-closed validation boundary for every incoming work source."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol, cast

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from jsonschema.exceptions import SchemaError  # type: ignore[import-untyped]

from marketing_agents.application.policies.runtime_guard import RuntimePolicyViolation
from marketing_agents.domain.admission import AdmissionEnvelope
from marketing_agents.domain.canonical_json import CanonicalJsonError, canonical_json_bytes
from marketing_agents.domain.data_classification import DataClassification
from marketing_agents.domain.entities._validation import require_digest, require_id
from marketing_agents.domain.enums import TriggerKind, WorkMode
from marketing_agents.domain.validation import frozen_json_mapping
from marketing_agents.security.redaction import redact, redaction_classification


class IncomingWorkValidationError(ValueError):
    """Stable, payload-safe rejection before work admission can have side effects."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class CampaignBriefPolicy(StrEnum):
    OPTIONAL = "optional"
    REQUIRED = "required"
    FORBIDDEN = "forbidden"


class IncomingTemplateSource(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def supported_trigger_types(self) -> tuple[str, ...]: ...


class IncomingInstanceSource(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def template_id(self) -> str: ...

    @property
    def enabled(self) -> bool: ...

    @property
    def configuration_revision(self) -> int: ...


class InputPolicyGuard(Protocol):
    def validate_input(self, payload: Any, schema: Mapping[str, Any]) -> None: ...


@dataclass(frozen=True, slots=True)
class ConfiguredIncomingTrigger:
    id: str
    instance_id: str
    kind: TriggerKind
    source: str
    workflow_ids: tuple[str, ...]
    enabled: bool = True

    def __post_init__(self) -> None:
        require_id(self.id, "configured trigger ID")
        require_id(self.instance_id, "configured trigger instance ID")
        require_id(self.source, "configured trigger source")
        if not isinstance(self.kind, TriggerKind):
            raise ValueError("configured trigger kind is invalid")
        if not isinstance(self.enabled, bool):
            raise ValueError("configured trigger enabled state must be boolean")
        if not self.workflow_ids or len(self.workflow_ids) != len(set(self.workflow_ids)):
            raise ValueError("configured trigger workflows must be nonempty and unique")
        for workflow_id in self.workflow_ids:
            require_id(workflow_id, "configured trigger workflow ID")


@dataclass(frozen=True, slots=True)
class WorkflowAdmissionDefinition:
    id: str
    eligible_template_ids: tuple[str, ...]
    eligible_trigger_kinds: tuple[TriggerKind, ...]
    allowed_modes: tuple[WorkMode, ...]
    input_schema_ids_by_template: Mapping[str, str]
    campaign_brief_policy: CampaignBriefPolicy = CampaignBriefPolicy.OPTIONAL
    enabled: bool = True

    def __post_init__(self) -> None:
        require_id(self.id, "workflow ID")
        for values, field_name in (
            (self.eligible_template_ids, "workflow template IDs"),
            (self.eligible_trigger_kinds, "workflow trigger kinds"),
            (self.allowed_modes, "workflow modes"),
        ):
            if not values or len(values) != len(set(values)):
                raise ValueError(f"{field_name} must be nonempty and unique")
        for template_id in self.eligible_template_ids:
            require_id(template_id, "workflow template ID")
        if any(not isinstance(item, TriggerKind) for item in self.eligible_trigger_kinds):
            raise ValueError("workflow trigger kind is invalid")
        if any(not isinstance(item, WorkMode) for item in self.allowed_modes):
            raise ValueError("workflow mode is invalid")
        if not isinstance(self.campaign_brief_policy, CampaignBriefPolicy):
            raise ValueError("workflow campaign brief policy is invalid")
        if not isinstance(self.enabled, bool):
            raise ValueError("workflow enabled state must be boolean")
        schema_ids = dict(self.input_schema_ids_by_template)
        if set(schema_ids) != set(self.eligible_template_ids):
            raise ValueError("workflow must bind one input schema ID per eligible template")
        for schema_id in schema_ids.values():
            require_id(schema_id, "workflow input schema ID")
        object.__setattr__(
            self,
            "input_schema_ids_by_template",
            MappingProxyType(schema_ids),
        )


@dataclass(frozen=True, slots=True)
class CampaignBriefRevision:
    id: str
    revision: int
    enabled: bool = True

    def __post_init__(self) -> None:
        require_id(self.id, "campaign brief ID")
        if (
            not isinstance(self.revision, int)
            or isinstance(self.revision, bool)
            or self.revision < 1
        ):
            raise ValueError("campaign brief revision must be positive")
        if not isinstance(self.enabled, bool):
            raise ValueError("campaign brief enabled state must be boolean")


@dataclass(frozen=True, slots=True)
class WorkflowAdmissionSnapshot:
    catalog_hash: str
    instance_id: str
    template_id: str
    instance_configuration_revision: int
    trigger_id: str
    workflow_id: str
    input_schema_id: str
    input_schema_hash: str

    def __post_init__(self) -> None:
        if not self.catalog_hash.startswith("catalog-sha256-v1:"):
            raise ValueError("admission snapshot catalog hash version is invalid")
        require_digest(
            self.catalog_hash.removeprefix("catalog-sha256-v1:"),
            "admission snapshot catalog hash",
        )
        for value, field_name in (
            (self.instance_id, "admission snapshot instance ID"),
            (self.template_id, "admission snapshot template ID"),
            (self.trigger_id, "admission snapshot trigger ID"),
            (self.workflow_id, "admission snapshot workflow ID"),
            (self.input_schema_id, "admission snapshot input schema ID"),
        ):
            require_id(value, field_name)
        if (
            not isinstance(self.instance_configuration_revision, int)
            or isinstance(self.instance_configuration_revision, bool)
            or self.instance_configuration_revision < 1
        ):
            raise ValueError("admission snapshot configuration revision must be positive")
        if not self.input_schema_hash.startswith("schema-sha256-v1:"):
            raise ValueError("admission snapshot schema hash version is invalid")
        require_digest(
            self.input_schema_hash.removeprefix("schema-sha256-v1:"),
            "admission snapshot input schema hash",
        )


_VALIDATION_SEAL = object()


@dataclass(frozen=True, slots=True, init=False)
class ValidatedIncomingWork:
    """Validator-issued marker required by the public admission boundary."""

    envelope: AdmissionEnvelope
    snapshot: WorkflowAdmissionSnapshot
    redacted_input_projection: Mapping[str, Any] = field(repr=False)
    input_classification: DataClassification
    _seal: object = field(repr=False, compare=False)
    _integrity_hash: str = field(repr=False, compare=False)

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("ValidatedIncomingWork can only be issued by IncomingWorkValidator")


def _issue_validated(
    envelope: AdmissionEnvelope,
    snapshot: WorkflowAdmissionSnapshot,
    payload: Mapping[str, Any],
    schema: Mapping[str, Any],
) -> ValidatedIncomingWork:
    try:
        redacted = redact(payload, schema=schema)
        projection = redacted if isinstance(redacted, Mapping) else {"$redacted_input": redacted}
        frozen_projection = frozen_json_mapping(projection, "redacted input projection")
        classification = redaction_classification(payload, schema=schema)
    except (CanonicalJsonError, TypeError, ValueError) as exc:
        raise IncomingWorkValidationError(
            "input_redaction_invalid",
            "incoming payload could not produce a safe redacted projection",
        ) from exc
    if classification is DataClassification.SECRET:
        raise IncomingWorkValidationError(
            "input_secret_not_retainable",
            "incoming payload contains data classified as non-retainable secret material",
        )
    value = object.__new__(ValidatedIncomingWork)
    object.__setattr__(value, "envelope", envelope)
    object.__setattr__(value, "snapshot", snapshot)
    object.__setattr__(value, "redacted_input_projection", frozen_projection)
    object.__setattr__(value, "input_classification", classification)
    object.__setattr__(value, "_seal", _VALIDATION_SEAL)
    object.__setattr__(
        value,
        "_integrity_hash",
        _validated_integrity_hash(
            envelope,
            snapshot,
            frozen_projection,
            classification,
        ),
    )
    return value


def _validated_integrity_hash(
    envelope: AdmissionEnvelope,
    snapshot: WorkflowAdmissionSnapshot,
    redacted_input_projection: Mapping[str, Any],
    input_classification: DataClassification,
) -> str:
    projection = {
        "envelope": {
            "source": envelope.source,
            "event_id": envelope.event_id,
            "instance_id": envelope.instance_id,
            "trigger_id": envelope.trigger_id,
            "workflow_id": envelope.workflow_id,
            "mode": envelope.mode.value,
            "brief_id": envelope.brief_id,
            "brief_revision": envelope.brief_revision,
            "configuration_revision": envelope.configuration_revision,
            "admitted_payload": envelope.admitted_payload,
        },
        "snapshot": {
            "catalog_hash": snapshot.catalog_hash,
            "instance_id": snapshot.instance_id,
            "template_id": snapshot.template_id,
            "instance_configuration_revision": snapshot.instance_configuration_revision,
            "trigger_id": snapshot.trigger_id,
            "workflow_id": snapshot.workflow_id,
            "input_schema_id": snapshot.input_schema_id,
            "input_schema_hash": snapshot.input_schema_hash,
        },
        "redacted_input_projection": redacted_input_projection,
        "input_classification": input_classification.value,
    }
    digest = hashlib.sha256(
        b"validated-incoming-work:v1\x00" + canonical_json_bytes(projection)
    ).hexdigest()
    return f"validated-incoming-sha256-v1:{digest}"


def _validated_parts(
    value: object,
) -> tuple[
    AdmissionEnvelope,
    WorkflowAdmissionSnapshot,
    Mapping[str, Any],
    DataClassification,
]:
    if (
        type(value) is not ValidatedIncomingWork
        or getattr(value, "_seal", None) is not _VALIDATION_SEAL
    ):
        raise IncomingWorkValidationError(
            "incoming_work_not_validated",
            "work admission requires a validator-issued incoming-work marker",
        )
    envelope = getattr(value, "envelope", None)
    snapshot = getattr(value, "snapshot", None)
    redacted_input_projection = getattr(value, "redacted_input_projection", None)
    input_classification = getattr(value, "input_classification", None)
    if type(envelope) is not AdmissionEnvelope or type(snapshot) is not WorkflowAdmissionSnapshot:
        raise IncomingWorkValidationError(
            "incoming_work_not_validated",
            "work admission requires a complete validator-issued incoming-work marker",
        )
    if not isinstance(redacted_input_projection, Mapping) or not isinstance(
        input_classification,
        DataClassification,
    ):
        raise IncomingWorkValidationError(
            "incoming_work_not_validated",
            "work admission requires a complete redacted incoming-work projection",
        )
    try:
        expected_integrity_hash = _validated_integrity_hash(
            envelope,
            snapshot,
            redacted_input_projection,
            input_classification,
        )
    except (CanonicalJsonError, TypeError, ValueError) as exc:
        raise IncomingWorkValidationError(
            "incoming_work_not_validated",
            "validator-issued incoming-work marker integrity is invalid",
        ) from exc
    integrity_hash = getattr(value, "_integrity_hash", None)
    if not isinstance(integrity_hash, str) or not hmac.compare_digest(
        integrity_hash,
        expected_integrity_hash,
    ):
        raise IncomingWorkValidationError(
            "incoming_work_not_validated",
            "validator-issued incoming-work marker integrity is invalid",
        )
    if (
        snapshot.instance_id != envelope.instance_id
        or snapshot.trigger_id != envelope.trigger_id
        or snapshot.workflow_id != envelope.workflow_id
        or snapshot.instance_configuration_revision != envelope.configuration_revision
    ):
        raise IncomingWorkValidationError(
            "admission_snapshot_invalid",
            "incoming work no longer matches its validator-issued admission snapshot",
        )
    return envelope, snapshot, redacted_input_projection, input_classification


def _validated_envelope(value: object) -> AdmissionEnvelope:
    return _validated_parts(value)[0]


def _validated_snapshot(value: object) -> WorkflowAdmissionSnapshot:
    return _validated_parts(value)[1]


def _validated_redacted_input(
    value: object,
) -> tuple[Mapping[str, Any], DataClassification]:
    parts = _validated_parts(value)
    return parts[2], parts[3]


def _unique_by_id[RecordT](records: Sequence[RecordT], label: str) -> dict[str, RecordT]:
    indexed: dict[str, RecordT] = {}
    for record in records:
        identifier = getattr(record, "id", None)
        if not isinstance(identifier, str) or identifier in indexed:
            raise ValueError(f"{label} IDs must be valid and unique")
        indexed[identifier] = record
    return indexed


def _schema_identity(schema: Mapping[str, Any]) -> tuple[str, str, dict[str, Any]]:
    schema_id = schema.get("$id")
    if not isinstance(schema_id, str):
        raise IncomingWorkValidationError(
            "input_schema_identity_missing",
            "the selected template input schema has no stable ID",
        )
    try:
        require_id(schema_id, "input schema ID")
        encoded = canonical_json_bytes(schema)
        plain_schema = json.loads(encoded)
        if not isinstance(plain_schema, dict):
            raise ValueError("input schema must be a JSON object")
        Draft202012Validator.check_schema(plain_schema)
    except (ValueError, CanonicalJsonError, SchemaError) as exc:
        raise IncomingWorkValidationError(
            "input_schema_invalid",
            "the selected template input schema is not canonical strict JSON",
        ) from exc
    digest = hashlib.sha256(encoded).hexdigest()
    return schema_id, f"schema-sha256-v1:{digest}", cast(dict[str, Any], plain_schema)


def _plain_json_payload(envelope: AdmissionEnvelope) -> dict[str, Any]:
    try:
        value = json.loads(canonical_json_bytes(envelope.admitted_payload))
    except (ValueError, CanonicalJsonError) as exc:
        raise IncomingWorkValidationError(
            "invalid_json",
            "incoming payload is not canonical strict JSON",
        ) from exc
    if not isinstance(value, dict):
        raise IncomingWorkValidationError("invalid_json", "incoming payload must be a JSON object")
    return cast(dict[str, Any], value)


def _copy_input_schemas(
    schemas: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Mapping[str, Any]]:
    copied: dict[str, Mapping[str, Any]] = {}
    try:
        for template_id, schema in schemas.items():
            require_id(template_id, "input schema template ID")
            plain = json.loads(canonical_json_bytes(schema))
            if not isinstance(plain, dict):
                raise ValueError("compiled input schema must be a JSON object")
            copied[template_id] = cast(dict[str, Any], plain)
    except (ValueError, CanonicalJsonError) as exc:
        raise ValueError("compiled input schemas must be canonical strict JSON objects") from exc
    return MappingProxyType(copied)


class IncomingWorkValidator:
    """Resolve trusted intake bindings and issue a sealed marker after every check."""

    def __init__(
        self,
        *,
        catalog_hash: str,
        templates: Sequence[IncomingTemplateSource],
        instances: Sequence[IncomingInstanceSource],
        input_schemas_by_template: Mapping[str, Mapping[str, Any]],
        triggers: Sequence[ConfiguredIncomingTrigger],
        workflows: Sequence[WorkflowAdmissionDefinition],
        campaign_brief_revisions: Collection[CampaignBriefRevision],
        guard: InputPolicyGuard,
    ) -> None:
        if not catalog_hash.startswith("catalog-sha256-v1:"):
            raise ValueError("incoming-work catalog hash version is invalid")
        require_digest(
            catalog_hash.removeprefix("catalog-sha256-v1:"),
            "incoming-work catalog hash",
        )
        self._catalog_hash = catalog_hash
        self._templates = _unique_by_id(templates, "template")
        self._instances = _unique_by_id(instances, "instance")
        self._schemas = _copy_input_schemas(input_schemas_by_template)
        self._triggers = _unique_by_id(triggers, "trigger")
        self._workflows = _unique_by_id(workflows, "workflow")
        brief_index: dict[tuple[str, int], CampaignBriefRevision] = {}
        for brief in campaign_brief_revisions:
            key = (brief.id, brief.revision)
            if key in brief_index:
                raise ValueError("campaign brief revision keys must be unique")
            brief_index[key] = brief
        self._briefs = MappingProxyType(brief_index)
        self._guard = guard

    def validate(
        self,
        envelope: AdmissionEnvelope,
        *,
        expected_snapshot: WorkflowAdmissionSnapshot | None = None,
    ) -> ValidatedIncomingWork:
        if not isinstance(envelope, AdmissionEnvelope):
            raise IncomingWorkValidationError(
                "incoming_envelope_invalid",
                "incoming work must use the typed admission envelope",
            )
        if expected_snapshot is not None and not isinstance(
            expected_snapshot, WorkflowAdmissionSnapshot
        ):
            raise IncomingWorkValidationError(
                "admission_snapshot_invalid",
                "worker revalidation requires a trusted admission snapshot",
            )
        instance = self._instances.get(envelope.instance_id)
        if instance is None:
            raise IncomingWorkValidationError(
                "instance_unknown", "agent instance is not registered"
            )
        if not instance.enabled:
            raise IncomingWorkValidationError("instance_disabled", "agent instance is disabled")
        template = self._templates.get(instance.template_id)
        if template is None:
            raise IncomingWorkValidationError(
                "template_unknown",
                "agent instance references an unavailable template",
            )
        if envelope.configuration_revision != instance.configuration_revision:
            raise IncomingWorkValidationError(
                "instance_configuration_mismatch",
                "incoming work does not match the active instance configuration revision",
            )

        workflow = self._workflows.get(envelope.workflow_id)
        if workflow is None:
            raise IncomingWorkValidationError("workflow_unknown", "workflow is not registered")
        if not workflow.enabled:
            raise IncomingWorkValidationError("workflow_disabled", "workflow is disabled")
        if template.id not in workflow.eligible_template_ids:
            raise IncomingWorkValidationError(
                "workflow_template_mismatch",
                "workflow is not eligible for the selected template",
            )
        if envelope.mode not in workflow.allowed_modes:
            raise IncomingWorkValidationError(
                "work_mode_not_allowed",
                "execution mode is not allowed by the selected workflow",
            )

        trigger = self._triggers.get(envelope.trigger_id)
        if trigger is None:
            raise IncomingWorkValidationError("trigger_unknown", "trigger is not configured")
        if not trigger.enabled:
            raise IncomingWorkValidationError("trigger_disabled", "trigger is disabled")
        if trigger.instance_id != instance.id:
            raise IncomingWorkValidationError(
                "trigger_instance_mismatch",
                "trigger is not bound to the selected instance",
            )
        if trigger.source != envelope.source:
            raise IncomingWorkValidationError(
                "trigger_source_mismatch",
                "trigger is not bound to the incoming source",
            )
        if envelope.workflow_id not in trigger.workflow_ids:
            raise IncomingWorkValidationError(
                "trigger_workflow_mismatch",
                "trigger is not bound to the requested workflow",
            )
        if trigger.kind.value not in template.supported_trigger_types:
            raise IncomingWorkValidationError(
                "template_trigger_unsupported",
                "selected template does not support the configured trigger kind",
            )
        if trigger.kind not in workflow.eligible_trigger_kinds:
            raise IncomingWorkValidationError(
                "workflow_trigger_mismatch",
                "workflow is not eligible for the configured trigger kind",
            )

        brief_key = (
            None
            if envelope.brief_id is None or envelope.brief_revision is None
            else (envelope.brief_id, envelope.brief_revision)
        )
        if workflow.campaign_brief_policy is CampaignBriefPolicy.REQUIRED and brief_key is None:
            raise IncomingWorkValidationError(
                "campaign_brief_required",
                "workflow requires a campaign brief revision",
            )
        if (
            workflow.campaign_brief_policy is CampaignBriefPolicy.FORBIDDEN
            and brief_key is not None
        ):
            raise IncomingWorkValidationError(
                "campaign_brief_forbidden",
                "workflow does not accept a campaign brief",
            )
        if brief_key is not None:
            brief = self._briefs.get(brief_key)
            if brief is None:
                raise IncomingWorkValidationError(
                    "campaign_brief_unknown",
                    "campaign brief revision is not registered",
                )
            if not brief.enabled:
                raise IncomingWorkValidationError(
                    "campaign_brief_disabled",
                    "campaign brief revision is disabled",
                )

        schema = self._schemas.get(template.id)
        if schema is None:
            raise IncomingWorkValidationError(
                "input_schema_missing",
                "selected template has no compiled input schema",
            )
        schema_id, schema_hash, plain_schema = _schema_identity(schema)
        if workflow.input_schema_ids_by_template[template.id] != schema_id:
            raise IncomingWorkValidationError(
                "workflow_schema_mismatch",
                "workflow input schema does not match the selected template",
            )
        snapshot = WorkflowAdmissionSnapshot(
            catalog_hash=self._catalog_hash,
            instance_id=instance.id,
            template_id=template.id,
            instance_configuration_revision=instance.configuration_revision,
            trigger_id=trigger.id,
            workflow_id=workflow.id,
            input_schema_id=schema_id,
            input_schema_hash=schema_hash,
        )
        if expected_snapshot is not None:
            self._compare_snapshot(snapshot, expected_snapshot)

        payload = _plain_json_payload(envelope)
        try:
            self._guard.validate_input(payload, plain_schema)
        except RuntimePolicyViolation as exc:
            raise IncomingWorkValidationError(exc.code, str(exc)) from exc
        return _issue_validated(envelope, snapshot, payload, plain_schema)

    @staticmethod
    def _compare_snapshot(
        current: WorkflowAdmissionSnapshot,
        expected: WorkflowAdmissionSnapshot,
    ) -> None:
        comparisons = (
            (current.instance_id, expected.instance_id, "instance_drift"),
            (current.template_id, expected.template_id, "template_drift"),
            (
                current.instance_configuration_revision,
                expected.instance_configuration_revision,
                "instance_configuration_drift",
            ),
            (current.trigger_id, expected.trigger_id, "trigger_drift"),
            (current.workflow_id, expected.workflow_id, "workflow_drift"),
            (current.input_schema_id, expected.input_schema_id, "input_schema_identity_drift"),
            (current.input_schema_hash, expected.input_schema_hash, "input_schema_drift"),
            (current.catalog_hash, expected.catalog_hash, "catalog_drift"),
        )
        for actual, trusted, code in comparisons:
            if actual != trusted:
                raise IncomingWorkValidationError(
                    code,
                    "incoming work no longer matches its trusted admission snapshot",
                )

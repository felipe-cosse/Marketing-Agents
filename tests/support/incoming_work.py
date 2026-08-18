"""Issue real validator-sealed work for requirements predating ORCH-02."""

from __future__ import annotations

from dataclasses import dataclass

from marketing_agents.application.policies.runtime_guard import (
    CapabilityPolicy,
    RuntimePolicyGuard,
    RuntimePolicySnapshot,
)
from marketing_agents.application.services.incoming_work_validation import (
    CampaignBriefRevision,
    ConfiguredIncomingTrigger,
    IncomingWorkValidator,
    ValidatedIncomingWork,
    WorkflowAdmissionDefinition,
)
from marketing_agents.domain.admission import AdmissionEnvelope
from marketing_agents.domain.enums import TriggerKind, WorkMode

TEST_CATALOG_HASH = "catalog-sha256-v1:" + ("a" * 64)
TEST_TEMPLATE_ID = "tpl.test.trusted.incoming-work"
TEST_SCHEMA_ID = "urn:marketing-agents:test:trusted-incoming-work:input"


@dataclass(frozen=True, slots=True)
class _Template:
    id: str = TEST_TEMPLATE_ID
    supported_trigger_types: tuple[str, ...] = ("manual",)


@dataclass(frozen=True, slots=True)
class _Instance:
    id: str
    template_id: str
    enabled: bool
    configuration_revision: int


def _guard() -> RuntimePolicyGuard:
    return RuntimePolicyGuard(
        RuntimePolicySnapshot(
            allowed_capabilities=(
                CapabilityPolicy(
                    capability_id="test.read",
                    effect="read",
                    connector_family="test",
                ),
            ),
            input_max_bytes=1_048_576,
            output_max_bytes=1_048_576,
            max_json_depth=64,
            max_content_parts=256,
            max_content_characters=1_000_000,
            max_model_calls=1,
            max_tool_calls=1,
            rate_window_max_calls=1,
            rate_window_seconds=60,
            step_timeout_seconds=10,
            run_timeout_seconds=60,
        )
    )


def validate_incoming_for_test(
    envelope: AdmissionEnvelope,
    *,
    catalog_hash: str = TEST_CATALOG_HASH,
) -> ValidatedIncomingWork:
    """Validate one synthetic trusted binding without exposing a production bypass."""

    brief_revisions = (
        ()
        if envelope.brief_id is None or envelope.brief_revision is None
        else (CampaignBriefRevision(envelope.brief_id, envelope.brief_revision),)
    )
    validator = IncomingWorkValidator(
        catalog_hash=catalog_hash,
        templates=(_Template(),),
        instances=(
            _Instance(
                id=envelope.instance_id,
                template_id=TEST_TEMPLATE_ID,
                enabled=True,
                configuration_revision=envelope.configuration_revision,
            ),
        ),
        input_schemas_by_template={
            TEST_TEMPLATE_ID: {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": TEST_SCHEMA_ID,
                "type": "object",
            }
        },
        triggers=(
            ConfiguredIncomingTrigger(
                id=envelope.trigger_id,
                instance_id=envelope.instance_id,
                kind=TriggerKind.MANUAL,
                source=envelope.source,
                workflow_ids=(envelope.workflow_id,),
            ),
        ),
        workflows=(
            WorkflowAdmissionDefinition(
                id=envelope.workflow_id,
                eligible_template_ids=(TEST_TEMPLATE_ID,),
                eligible_trigger_kinds=(TriggerKind.MANUAL,),
                allowed_modes=(WorkMode.DRY_RUN, WorkMode.MOCK_EXECUTION),
                input_schema_ids_by_template={TEST_TEMPLATE_ID: TEST_SCHEMA_ID},
            ),
        ),
        campaign_brief_revisions=brief_revisions,
        guard=_guard(),
    )
    return validator.validate(envelope)

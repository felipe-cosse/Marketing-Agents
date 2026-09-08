"""Advance a persisted occurrence through the common atomic intake service."""

from dataclasses import dataclass

from marketing_agents.application.policies.runtime_guard import (
    CapabilityPolicy,
    RuntimePolicyGuard,
    RuntimePolicySnapshot,
)
from marketing_agents.application.services.incoming_work_validation import (
    CampaignBriefPolicy,
    ConfiguredIncomingTrigger,
    IncomingWorkValidator,
    WorkflowAdmissionDefinition,
)
from marketing_agents.application.services.schedule_claiming import ScheduleClaimService
from marketing_agents.application.services.schedule_occurrence_ingress import (
    ScheduleOccurrenceCommand,
)
from marketing_agents.application.services.schedule_processing import ScheduleClaimProcessingService
from marketing_agents.domain.enums import TriggerKind, WorkMode
from marketing_agents.domain.schedule_occurrence_identity import schedule_occurrence_id
from marketing_agents.domain.validation import require_id
from marketing_agents.infrastructure.scheduling import CroniterRecurrenceCalculator

from .composition import LocalRuntime


@dataclass(frozen=True, slots=True)
class _ScheduledInstance:
    id: str
    template_id: str
    enabled: bool
    configuration_revision: int


class SchedulerWorker:
    def __init__(self, runtime: LocalRuntime, worker_id: str) -> None:
        require_id(worker_id, "scheduler worker ID")
        self.runtime = runtime
        self.worker_id = worker_id
        self.stopping = False

    def stop_claiming(self) -> None:
        self.stopping = True

    async def drain_once(self) -> bool:
        if self.stopping:
            return False
        dependencies = self.runtime.dependencies
        claim = await ScheduleClaimService(dependencies).claim_due_once(lease_owner=self.worker_id)
        if claim is None:
            return False
        async with dependencies.unit_of_work() as unit_of_work:
            schedule = await unit_of_work.schedules.get(claim.schedule_id)
            configuration = (
                None
                if schedule is None
                else await unit_of_work.configurations.get(schedule.instance_id)
            )
        if schedule is None or configuration is None or not configuration.enabled:
            raise ValueError("runtime_schedule_configuration_unavailable")
        catalog = self.runtime.catalog
        instance = next(item for item in catalog.instances if item.id == schedule.instance_id)
        template = next(item for item in catalog.templates if item.id == instance.template_id)
        policy = template.budget_policy
        guard = RuntimePolicyGuard(
            RuntimePolicySnapshot(
                allowed_capabilities=tuple(
                    CapabilityPolicy(
                        capability_id=item.id,
                        effect=item.effect,
                        connector_family=item.connector_family,
                    )
                    for item in catalog.tool_capabilities
                    if item.id in template.allowed_tool_capability_ids
                ),
                input_max_bytes=policy.max_input_bytes,
                max_input_field_bytes=policy.max_input_field_bytes,
                output_max_bytes=policy.max_output_bytes,
                max_json_depth=16,
                max_content_parts=1,
                max_content_characters=policy.max_input_bytes,
                max_model_calls=policy.max_model_calls,
                max_tool_calls=policy.max_tool_calls,
                rate_window_max_calls=template.rate_limit_policy.max_calls,
                rate_window_seconds=template.rate_limit_policy.window_seconds,
                step_timeout_seconds=template.timeout_policy.step_seconds,
                run_timeout_seconds=template.timeout_policy.run_seconds,
            )
        )
        validator = IncomingWorkValidator(
            catalog_hash=catalog.content_hash,
            templates=(template,),
            instances=(
                _ScheduledInstance(
                    instance.id,
                    template.id,
                    configuration.enabled,
                    configuration.configuration_revision,
                ),
            ),
            input_schemas_by_template={template.id: catalog.input_schema_by_template[template.id]},
            triggers=(
                ConfiguredIncomingTrigger(
                    id=schedule.trigger_id,
                    instance_id=instance.id,
                    kind=TriggerKind.SCHEDULE,
                    source="schedule",
                    workflow_ids=(schedule.workflow_id,),
                ),
            ),
            workflows=(
                WorkflowAdmissionDefinition(
                    id=schedule.workflow_id,
                    eligible_template_ids=(template.id,),
                    eligible_trigger_kinds=(TriggerKind.SCHEDULE,),
                    allowed_modes=(WorkMode.DRY_RUN,),
                    input_schema_ids_by_template={template.id: template.input_schema_id},
                    campaign_brief_policy=CampaignBriefPolicy.FORBIDDEN,
                ),
            ),
            campaign_brief_revisions=(),
            guard=guard,
        )
        occurrence_id = schedule_occurrence_id(
            schedule.id, claim.scheduled_for_utc, recurrence_version=schedule.recurrence_version
        )
        await ScheduleClaimProcessingService(
            dependencies,
            self.runtime.digest_key,
            validator,
            CroniterRecurrenceCalculator(),
            current_catalog_hash=catalog.content_hash,
        ).process_claimed_once(
            ScheduleOccurrenceCommand(
                claim=claim,
                mode=WorkMode.DRY_RUN,
                configuration_revision=configuration.configuration_revision,
                admitted_payload={
                    "request_id": occurrence_id[-64:],
                    "source_content": "Scheduled local mock check. No external source was queried.",
                },
            )
        )
        return True

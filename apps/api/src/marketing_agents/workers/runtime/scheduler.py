"""Advance a persisted occurrence through the common atomic intake service."""

from dataclasses import dataclass

from marketing_agents.application.policies.runtime_guard import (
    CapabilityPolicy,
    RuntimePolicyGuard,
    RuntimePolicySnapshot,
)
from marketing_agents.application.services.incoming_work_validation import (
    ConfiguredIncomingTrigger,
    IncomingWorkValidator,
)
from marketing_agents.application.services.schedule_claiming import ScheduleClaimService
from marketing_agents.application.services.schedule_configuration import verify_scheduled_input
from marketing_agents.application.services.schedule_occurrence_ingress import (
    ScheduleOccurrenceCommand,
)
from marketing_agents.application.services.schedule_processing import (
    ScheduleClaimProcessingError,
    ScheduleClaimProcessingService,
)
from marketing_agents.domain.entities import ScheduleClaim
from marketing_agents.domain.enums import TriggerKind
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
        claim = await ScheduleClaimService(
            dependencies, require_configuration_binding=True
        ).claim_due_once(lease_owner=self.worker_id)
        if claim is None:
            return False
        async with dependencies.unit_of_work() as unit_of_work:
            schedule = await unit_of_work.schedules.get(claim.schedule_id)
            configuration = (
                None
                if schedule is None
                else await unit_of_work.configurations.get(schedule.instance_id)
            )
        if (
            schedule is None
            or configuration is None
            or not configuration.enabled
            or not schedule.enabled
            or schedule.configuration_revision != configuration.configuration_revision
            or configuration.scheduled_input is None
            or configuration.scheduled_input.authority is None
            or not any(
                item.kind is TriggerKind.SCHEDULE and item.enabled
                for item in configuration.trigger_bindings
            )
        ):
            if await self._claim_is_obsolete(claim):
                return True
            raise ValueError("runtime_schedule_configuration_unavailable")
        snapshot = configuration.scheduled_input
        verify_scheduled_input(schedule.instance_id, snapshot, self.runtime.digest_key)
        catalog = self.runtime.catalog
        instance = next(item for item in catalog.instances if item.id == schedule.instance_id)
        template = next(item for item in catalog.templates if item.id == instance.template_id)
        workflow = self.runtime.workflows.require_match(
            schedule.workflow_id,
            instance_id=instance.id,
            template_id=template.id,
            trigger_kind=TriggerKind.SCHEDULE,
            mode=snapshot.mode,
            catalog_content_hash=catalog.content_hash,
        )
        authority = snapshot.authority
        if (
            authority is None
            or authority.workflow_id != workflow.id
            or authority.input_schema_id != workflow.input_schema_id
            or authority.input_schema_hash != workflow.input_schema_hash
            or authority.workflow_definition_hash != workflow.definition_hash
        ):
            raise ValueError("runtime_schedule_workflow_binding_invalid")
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
            input_schemas_by_template={template.id: workflow.input_schema},
            triggers=(
                ConfiguredIncomingTrigger(
                    id=schedule.trigger_id,
                    instance_id=instance.id,
                    kind=TriggerKind.SCHEDULE,
                    source="schedule",
                    workflow_ids=(schedule.workflow_id,),
                ),
            ),
            workflows=(workflow.admission_definition(),),
            campaign_brief_revisions=(),
            guard=guard,
        )
        try:
            await ScheduleClaimProcessingService(
                dependencies,
                self.runtime.digest_key,
                validator,
                CroniterRecurrenceCalculator(),
                current_catalog_hash=catalog.content_hash,
            ).process_claimed_once(
                ScheduleOccurrenceCommand(
                    claim=claim,
                    mode=snapshot.mode,
                    configuration_revision=configuration.configuration_revision,
                    admitted_payload=snapshot.admitted_payload,
                )
            )
        except ScheduleClaimProcessingError as error:
            if error.code in {
                "configuration_fence_lost",
                "claim_fence_lost",
                "schedule_changed_during_processing",
            } and await self._claim_is_obsolete(claim):
                return True
            raise
        return True

    async def _claim_is_obsolete(self, claim: ScheduleClaim) -> bool:
        """A stale claim is a normal no-op, not permission to ignore broken state.

        Configuration edits clear leases and advance the schedule version. Other
        workers can also consume/replace an expired claim. Only those monotonic,
        persisted facts (or verified expiry) permit continuing the worker loop.
        Repository integrity checks and current scheduled-input HMAC checks still
        execute and propagate; an unexplained fence failure remains an error.
        """
        dependencies = self.runtime.dependencies
        async with dependencies.unit_of_work() as unit_of_work:
            schedule = await unit_of_work.schedules.get(claim.schedule_id)
            if schedule is None:
                return False
            current_claim = await unit_of_work.schedules.get_claim(claim.schedule_id)
            configuration = await unit_of_work.configurations.get(schedule.instance_id)
        if configuration is None:
            return False
        if configuration.scheduled_input is not None:
            verify_scheduled_input(
                schedule.instance_id, configuration.scheduled_input, self.runtime.digest_key
            )
        if schedule.version > claim.version and current_claim != claim:
            return True
        return (
            schedule.version == claim.version
            and current_claim == claim
            and dependencies.utc_now() > claim.lease_expires_at_utc
        )

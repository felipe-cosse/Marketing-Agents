"""Create initial schedules only from a sealed recurrence calculation."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, replace
from datetime import datetime

from marketing_agents.application.orchestration.executable_workflows import (
    ExecutableWorkflowRegistry,
)
from marketing_agents.application.policies.json_schema import compile_json_schema
from marketing_agents.application.ports.instance_configuration import (
    InstanceConfigurationConstraints,
    InstanceConfigurationUnitOfWork,
)
from marketing_agents.application.ports.recurrence import RecurrenceCalculator
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.entities import Schedule
from marketing_agents.domain.enums import MisfirePolicy, TriggerKind
from marketing_agents.domain.instance_configuration import (
    InstanceConfiguration,
    ScheduledInput,
    ScheduledInputAuthority,
)
from marketing_agents.domain.runtime_policy import payload_fields_within_byte_limit
from marketing_agents.domain.schedule_occurrence_identity import (
    SCHEDULE_RECURRENCE_VERSION,
)
from marketing_agents.domain.validation import require_utc
from marketing_agents.security.admission_digest import admission_digest_key_version
from marketing_agents.security.digest_key import DigestKey

_INPUT_DOMAIN = b"marketing-agents:scheduled-input-binding:v1\x00"


def configured_schedule_id(instance_id: str) -> str:
    return "schedule.configuration." + hashlib.sha256(canonical_json_bytes(instance_id)).hexdigest()


def _input_digest(instance_id: str, snapshot: ScheduledInput, key: DigestKey) -> str:
    authority = snapshot.authority
    if authority is None:
        raise ScheduleConfigurationError("scheduled input is not bound")
    material = {
        "instance_id": instance_id,
        "workflow_id": authority.workflow_id,
        "workflow_definition_hash": authority.workflow_definition_hash,
        "input_schema_id": authority.input_schema_id,
        "input_schema_hash": authority.input_schema_hash,
        "digest_key_version": authority.digest_key_version,
        "mode": snapshot.mode.value,
        "input": snapshot.admitted_payload,
    }
    return hmac.new(
        key.bytes_for_digest(), _INPUT_DOMAIN + canonical_json_bytes(material), hashlib.sha256
    ).hexdigest()


def verify_scheduled_input(instance_id: str, snapshot: ScheduledInput, key: DigestKey) -> None:
    """Reject changed payload, routing, schema or key before a scheduler can admit it."""
    if (
        type(snapshot) is not ScheduledInput
        or snapshot.authority is None
        or snapshot.authority.digest_key_version != admission_digest_key_version(key)
        or not hmac.compare_digest(
            snapshot.authority.binding_digest, _input_digest(instance_id, snapshot, key)
        )
    ):
        raise ScheduleConfigurationError("scheduled input authority does not match")


def bind_scheduled_input(
    snapshot: ScheduledInput,
    constraints: InstanceConfigurationConstraints,
    workflows: ExecutableWorkflowRegistry,
    key: DigestKey,
) -> ScheduledInput:
    """Validate explicit business input against the actual executable schedule contract."""
    definition = workflows.for_catalog_role(constraints.template_id, TriggerKind.SCHEDULE)
    workflows.require_match(
        definition.id,
        instance_id=constraints.instance_id,
        template_id=constraints.template_id,
        trigger_kind=TriggerKind.SCHEDULE,
        mode=snapshot.mode,
        catalog_content_hash=workflows.catalog_content_hash,
    )
    raw = canonical_json_bytes(snapshot.admitted_payload)
    payload = json.loads(raw)
    if len(raw) > constraints.input_max_bytes or not payload_fields_within_byte_limit(
        payload, constraints.input_max_field_bytes
    ):
        raise ScheduleConfigurationError("scheduled input exceeds template limits")
    compile_json_schema(
        definition.input_schema, expected_schema_id=definition.input_schema_id
    ).validate(
        payload,
        pointer_root="/input",
        max_depth=16,
    )
    authority = ScheduledInputAuthority(
        workflow_id=definition.id,
        workflow_definition_hash=definition.definition_hash,
        input_schema_id=definition.input_schema_id,
        input_schema_hash=definition.input_schema_hash,
        digest_key_version=admission_digest_key_version(key),
        binding_digest="0" * 64,
    )
    bound = replace(snapshot, authority=authority)
    return replace(
        bound,
        authority=replace(
            authority, binding_digest=_input_digest(constraints.instance_id, bound, key)
        ),
    )


class ScheduleConfigurationError(RuntimeError):
    """Raised when a recurrence adapter violates the application contract."""


@dataclass(frozen=True, slots=True, kw_only=True)
class CreateScheduleCommand:
    id: str
    trigger_id: str
    instance_id: str
    workflow_id: str
    cron: str
    timezone: str
    misfire_policy: MisfirePolicy
    misfire_grace_seconds: int
    enabled: bool
    after_utc: datetime


class ScheduleConfigurationService:
    def __init__(self, recurrence: RecurrenceCalculator) -> None:
        self._recurrence = recurrence

    async def synchronize_in_uow(
        self,
        unit_of_work: InstanceConfigurationUnitOfWork,
        configuration: InstanceConfiguration,
        *,
        now: datetime,
    ) -> None:
        """Project one already-CAS-fenced configuration; never commit independently."""
        identifier = configured_schedule_id(configuration.instance_id)
        previous = await unit_of_work.schedules.get(identifier)
        parameters = configuration.schedule
        snapshot = configuration.scheduled_input
        authority = None if snapshot is None else snapshot.authority
        enabled = configuration.enabled and parameters is not None and authority is not None
        if previous is None:
            if not enabled or parameters is None or authority is None:
                return
            created = replace(
                self.create(
                    CreateScheduleCommand(
                        id=identifier,
                        trigger_id="trigger."
                        + hashlib.sha256(
                            canonical_json_bytes(
                                [
                                    configuration.instance_id,
                                    TriggerKind.SCHEDULE.value,
                                ]
                            )
                        ).hexdigest(),
                        instance_id=configuration.instance_id,
                        workflow_id=authority.workflow_id,
                        cron=parameters.cron,
                        timezone=parameters.timezone,
                        misfire_policy=parameters.misfire_policy,
                        misfire_grace_seconds=parameters.misfire_grace_seconds,
                        enabled=True,
                        after_utc=now,
                    )
                ),
                configuration_revision=configuration.configuration_revision,
            )
            inserted = await unit_of_work.schedules.add_or_get(created)
            if not inserted.inserted:
                raise ScheduleConfigurationError("schedule synchronization lost its creation fence")
            return
        next_run = previous.next_run_at_utc
        if (
            enabled
            and parameters is not None
            and (
                not previous.enabled
                or parameters.cron != previous.cron
                or parameters.timezone != previous.timezone
            )
        ):
            boundary = max(now, previous.last_scheduled_at_utc or now)
            next_run = self._recurrence.next_after(
                cron=parameters.cron,
                timezone=parameters.timezone,
                after_utc=boundary,
            )
            require_utc(next_run, "updated schedule occurrence")
            if next_run <= boundary:
                raise ScheduleConfigurationError("schedule recurrence must advance")
        replacement = replace(
            previous,
            enabled=enabled,
            next_run_at_utc=next_run,
            workflow_id=previous.workflow_id if authority is None else authority.workflow_id,
            cron=previous.cron if parameters is None else parameters.cron,
            timezone=previous.timezone if parameters is None else parameters.timezone,
            misfire_policy=previous.misfire_policy
            if parameters is None
            else parameters.misfire_policy,
            misfire_grace_seconds=(
                previous.misfire_grace_seconds
                if parameters is None
                else parameters.misfire_grace_seconds
            ),
            version=previous.version + 1,
            configuration_revision=configuration.configuration_revision,
        )
        if not await unit_of_work.schedules.compare_and_swap_configuration(previous, replacement):
            raise ScheduleConfigurationError("schedule synchronization lost its revision fence")

    def create(self, command: CreateScheduleCommand) -> Schedule:
        require_utc(command.after_utc, "schedule calculation boundary")
        next_run_at_utc = self._recurrence.next_after(
            cron=command.cron,
            timezone=command.timezone,
            after_utc=command.after_utc,
        )
        try:
            require_utc(next_run_at_utc, "calculated next scheduled time")
        except (AttributeError, ValueError) as exc:
            raise ScheduleConfigurationError(
                "recurrence calculator returned a non-UTC scheduled time"
            ) from exc
        if next_run_at_utc <= command.after_utc:
            raise ScheduleConfigurationError(
                "recurrence calculator must return a time strictly after the boundary"
            )
        return Schedule(
            id=command.id,
            trigger_id=command.trigger_id,
            instance_id=command.instance_id,
            workflow_id=command.workflow_id,
            cron=command.cron,
            timezone=command.timezone,
            next_run_at_utc=next_run_at_utc,
            misfire_policy=command.misfire_policy,
            misfire_grace_seconds=command.misfire_grace_seconds,
            enabled=command.enabled,
            recurrence_version=SCHEDULE_RECURRENCE_VERSION,
        )

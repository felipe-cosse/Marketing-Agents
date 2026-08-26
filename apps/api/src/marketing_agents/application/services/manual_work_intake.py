"""Authorized, schema-validated manual dry-run admission and receipt."""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from marketing_agents.application.orchestration.dependencies import (
    OrchestrationDependencies,
)
from marketing_agents.application.policies.manual_work_authorization import (
    ManualWorkAuthorizationError,
    authorize_manual_work_operator,
)
from marketing_agents.application.ports.manual_work import (
    ManualAdmissionBinding,
    ManualAdmissionResolutionError,
    ManualAdmissionResolver,
)
from marketing_agents.application.ports.unit_of_work import UnitOfWork
from marketing_agents.application.services.audit_events import AuditEventFactory
from marketing_agents.application.services.idempotent_work_receipt import (
    IdempotentWorkRunReceiptService,
    WorkRunReceiptDisposition,
    WorkRunReceiptResult,
)
from marketing_agents.application.services.incoming_work_validation import (
    IncomingWorkValidationError,
)
from marketing_agents.application.services.work_admission import WorkIdempotencyError
from marketing_agents.domain.admission import AdmissionEnvelope
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.entities import Run, WorkItem
from marketing_agents.domain.enums import WorkMode
from marketing_agents.domain.identity import AuthenticatedPrincipal
from marketing_agents.domain.validation import frozen_json_mapping, require_id
from marketing_agents.security.digest_key import DigestKey
from marketing_agents.security.redaction import SecretValue

_MANUAL_SOURCE = "manual"
_MANUAL_EVENT_ID_DOMAIN = b"marketing-agents:manual-event-id:hmac-sha256:v1\x00"
_MANUAL_EVENT_ID_PREFIX = "manual-event-hmac-sha256-v1:"
_MIN_IDEMPOTENCY_KEY_LENGTH = 8
_MAX_IDEMPOTENCY_KEY_LENGTH = 240
_MAX_MANUAL_PAYLOAD_DEPTH = 64
_SAFE_RESOLUTION_MESSAGES = {
    "instance_unknown": "agent instance is not registered",
    "instance_disabled": "agent instance is disabled",
    "work_mode_not_allowed": "execution mode is not allowed for manual work",
    "campaign_brief_unknown": "campaign brief is not registered",
    "campaign_brief_disabled": "campaign brief is disabled",
    "demo_scenario_unknown": "demo scenario is not registered",
    "demo_scenario_disabled": "demo scenario is disabled",
    "manual_trigger_unavailable": "agent instance has no enabled manual trigger",
    "manual_binding_unavailable": "manual admission binding is unavailable",
}


class ManualDryRunServiceError(ValueError):
    """Stable payload-safe rejection at the manual-work application boundary."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        pointer: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.pointer = _safe_input_pointer(pointer)


def _safe_input_pointer(pointer: object) -> str | None:
    if (
        type(pointer) is str
        and len(pointer) <= 1_000
        and re.fullmatch(r"/input(?:/[A-Za-z0-9_.-]{1,100}){0,64}", pointer) is not None
    ):
        return pointer
    return None


def _invalid_command(message: str) -> ManualDryRunServiceError:
    return ManualDryRunServiceError("manual_work_command_invalid", message)


def _require_bounded_payload_depth(value: object) -> None:
    """Reject pathological nesting without recursively walking caller-owned input."""

    stack: list[tuple[object, int, bool]] = [(value, 1, False)]
    active: set[int] = set()
    while stack:
        current, depth, exiting = stack.pop()
        mapping = isinstance(current, Mapping)
        sequence = isinstance(current, Sequence) and not isinstance(
            current,
            (str, bytes, bytearray),
        )
        if not mapping and not sequence:
            continue
        identity = id(current)
        if exiting:
            active.remove(identity)
            continue
        if depth > _MAX_MANUAL_PAYLOAD_DEPTH or identity in active:
            raise _invalid_command("manual work command is invalid")
        active.add(identity)
        stack.append((current, depth, True))
        children = (
            current.values() if isinstance(current, Mapping) else cast(Sequence[object], current)
        )
        stack.extend((child, depth + 1, False) for child in children)


def _validated_idempotency_key(value: SecretValue) -> str:
    if type(value) is not SecretValue:
        raise ManualDryRunServiceError(
            "manual_idempotency_key_invalid",
            "manual idempotency keys must use the redacted secret-value boundary",
        )
    raw = value.reveal()
    if (
        raw != raw.strip()
        or not (_MIN_IDEMPOTENCY_KEY_LENGTH <= len(raw) <= _MAX_IDEMPOTENCY_KEY_LENGTH)
        or re.fullmatch(r"[\x21-\x7e]{8,240}", raw) is None
    ):
        raise ManualDryRunServiceError(
            "manual_idempotency_key_invalid",
            "manual idempotency key format is invalid",
        )
    return raw


@dataclass(frozen=True, slots=True, kw_only=True)
class ManualDryRunCommand:
    """Transport-neutral manual input; routing and actor authority are absent."""

    instance_id: str
    input_payload: Mapping[str, Any] = field(repr=False)
    correlation_id: str
    mode: WorkMode = WorkMode.DRY_RUN
    idempotency_key: SecretValue | None = field(default=None, repr=False)
    campaign_brief_id: str | None = None
    demo_scenario_id: str | None = None

    def __post_init__(self) -> None:
        try:
            require_id(self.instance_id, "manual command instance ID")
            require_id(self.correlation_id, "manual command correlation ID")
            if type(self.mode) is not WorkMode:
                raise ValueError("manual command mode must use the exact WorkMode enum")
            if self.campaign_brief_id is not None:
                require_id(self.campaign_brief_id, "manual command campaign brief ID")
            if self.demo_scenario_id is not None:
                require_id(self.demo_scenario_id, "manual command demo scenario ID")
            _require_bounded_payload_depth(self.input_payload)
            try:
                frozen_payload = frozen_json_mapping(
                    self.input_payload,
                    "manual command input payload",
                )
            except RecursionError:
                raise _invalid_command("manual work command is invalid") from None
            if self.idempotency_key is not None:
                _validated_idempotency_key(self.idempotency_key)
        except ManualDryRunServiceError:
            raise
        except (TypeError, ValueError):
            raise _invalid_command("manual work command is invalid") from None
        object.__setattr__(self, "input_payload", frozen_payload)


@dataclass(frozen=True, slots=True)
class ManualDryRunResult:
    """Exact authoritative resource identities returned after commit."""

    work_item: WorkItem
    run: Run
    disposition: WorkRunReceiptDisposition
    event_id: str
    mode: WorkMode

    def __post_init__(self) -> None:
        if (
            type(self.work_item) is not WorkItem
            or type(self.run) is not Run
            or type(self.disposition) is not WorkRunReceiptDisposition
            or type(self.mode) is not WorkMode
        ):
            raise ValueError("manual dry-run result types are invalid")
        require_id(self.event_id, "manual result event ID")
        if (
            self.work_item.source != _MANUAL_SOURCE
            or self.work_item.event_id != self.event_id
            or self.work_item.mode is not self.mode
            or self.run.work_item_id != self.work_item.id
            or self.run.configuration_revision != self.work_item.configuration_revision
        ):
            raise ValueError("manual dry-run result resources are incoherent")


class ManualDryRunService:
    """Authorize, resolve, validate, and atomically receipt one manual submission."""

    def __init__(
        self,
        dependencies: OrchestrationDependencies,
        digest_key: DigestKey,
        resolver: ManualAdmissionResolver,
        *,
        current_catalog_hash: str,
    ) -> None:
        if type(dependencies) is not OrchestrationDependencies:
            raise ValueError("manual dry-run service requires exact orchestration dependencies")
        if type(digest_key) is not DigestKey:
            raise ValueError("manual dry-run service requires the exact digest key")
        if not callable(getattr(resolver, "resolve_in_uow", None)):
            raise ValueError("manual dry-run service requires an admission resolver")
        self._dependencies = dependencies
        self._digest_key = digest_key
        self._resolver = resolver
        self._receipt = IdempotentWorkRunReceiptService(
            dependencies,
            digest_key,
            current_catalog_hash=current_catalog_hash,
        )

    async def submit(
        self,
        command: ManualDryRunCommand,
        principal: AuthenticatedPrincipal,
    ) -> ManualDryRunResult:
        """Submit one manual event; authorization always precedes state resolution."""

        try:
            authorize_manual_work_operator(principal)
        except ManualWorkAuthorizationError as exc:
            raise ManualDryRunServiceError(exc.code, str(exc)) from None
        self._require_command(command)

        try:
            event_id = self._event_id(command.idempotency_key)
            audit_context = AuditContext.authenticated_user(
                principal.actor_id,
                authentication_method=principal.authentication_method.value,
                correlation_id=command.correlation_id,
            )
            async with self._dependencies.unit_of_work() as unit_of_work:
                binding = await self._resolver.resolve_in_uow(unit_of_work, command)
                self._require_binding(command, binding)
                envelope = AdmissionEnvelope(
                    source=binding.source,
                    event_id=event_id,
                    instance_id=binding.instance_id,
                    trigger_id=binding.trigger_id,
                    workflow_id=binding.workflow_id,
                    mode=command.mode,
                    brief_id=binding.brief_id,
                    brief_revision=binding.brief_revision,
                    configuration_revision=binding.configuration_revision,
                    admitted_payload=command.input_payload,
                )
                manual_attempt_id = self._dependencies.new_id("manual-ingress")
                try:
                    require_id(manual_attempt_id, "manual ingress attempt ID")
                except (TypeError, ValueError):
                    raise ManualDryRunServiceError(
                        "manual_work_unavailable",
                        "manual work intake is temporarily unavailable",
                    ) from None
                try:
                    incoming = binding.validator.validate(envelope)
                except IncomingWorkValidationError:
                    await unit_of_work.audits.append_global(
                        AuditEventFactory(audit_context).manual_schema_rejected(
                            envelope,
                            manual_attempt_id=manual_attempt_id,
                            occurred_at=self._dependencies.utc_now(),
                        )
                    )
                    await unit_of_work.commit()
                    raise
                collision_error: WorkIdempotencyError | None = None
                try:
                    receipt = await self._receipt.receive_in_uow(
                        unit_of_work,
                        incoming,
                        audit_context=audit_context,
                    )
                except WorkIdempotencyError as exc:
                    if exc.code != "idempotency_conflict":
                        raise
                    await self._append_collision_audit(
                        unit_of_work,
                        envelope=envelope,
                        error=exc,
                        audit_context=audit_context,
                        manual_attempt_id=manual_attempt_id,
                    )
                    await unit_of_work.commit()
                    collision_error = exc
                else:
                    result = self._result(command, binding, event_id, receipt)
                    await self._append_receipt_audit(
                        unit_of_work,
                        receipt=receipt,
                        audit_context=audit_context,
                        manual_attempt_id=manual_attempt_id,
                    )
                    await unit_of_work.commit()
            if collision_error is not None:
                raise collision_error
            return result
        except ManualDryRunServiceError:
            raise
        except ManualAdmissionResolutionError as exc:
            safe_message = _SAFE_RESOLUTION_MESSAGES.get(exc.code)
            if safe_message is None:
                raise ManualDryRunServiceError(
                    "manual_work_unavailable",
                    "manual work intake is temporarily unavailable",
                ) from None
            raise ManualDryRunServiceError(exc.code, safe_message) from None
        except IncomingWorkValidationError as exc:
            raise ManualDryRunServiceError(
                exc.code,
                str(exc),
                pointer=exc.pointer,
            ) from None
        except WorkIdempotencyError as exc:
            raise ManualDryRunServiceError(exc.code, str(exc)) from None
        except Exception:
            raise ManualDryRunServiceError(
                "manual_work_unavailable",
                "manual work intake is temporarily unavailable",
            ) from None

    @staticmethod
    def _require_command(command: ManualDryRunCommand) -> None:
        if type(command) is not ManualDryRunCommand:
            raise _invalid_command("manual work requires the exact typed command")
        try:
            command.__post_init__()
        except ManualDryRunServiceError:
            raise
        except (TypeError, ValueError):
            raise _invalid_command("manual work command is invalid") from None

    def _event_id(self, key: SecretValue | None) -> str:
        if key is None:
            event_id = self._dependencies.new_id("manual-event")
            try:
                require_id(event_id, "generated manual event ID")
            except (TypeError, ValueError):
                raise ManualDryRunServiceError(
                    "manual_work_unavailable",
                    "manual work intake is temporarily unavailable",
                ) from None
            return event_id
        raw = _validated_idempotency_key(key)
        digest = hmac.new(
            self._digest_key.bytes_for_digest(),
            _MANUAL_EVENT_ID_DOMAIN + raw.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        return _MANUAL_EVENT_ID_PREFIX + digest

    async def _append_receipt_audit(
        self,
        unit_of_work: UnitOfWork,
        *,
        receipt: WorkRunReceiptResult,
        audit_context: AuditContext,
        manual_attempt_id: str,
    ) -> None:
        occurred_at = self._dependencies.utc_now()
        factory = AuditEventFactory(audit_context)
        disposition = receipt.disposition.value
        received = factory.manual_received(
            receipt.work_item,
            receipt.run,
            manual_attempt_id=manual_attempt_id,
            disposition=disposition,
            occurred_at=occurred_at,
        )
        outcome = (
            factory.work_created(
                receipt.work_item,
                receipt.run,
                manual_attempt_id=manual_attempt_id,
                occurred_at=occurred_at,
            )
            if receipt.disposition is WorkRunReceiptDisposition.CREATED
            else factory.work_duplicate_returned(
                receipt.work_item,
                receipt.run,
                manual_attempt_id=manual_attempt_id,
                occurred_at=occurred_at,
            )
        )
        await unit_of_work.audits.append_many((received, outcome))

    async def _append_collision_audit(
        self,
        unit_of_work: UnitOfWork,
        *,
        envelope: AdmissionEnvelope,
        error: WorkIdempotencyError,
        audit_context: AuditContext,
        manual_attempt_id: str,
    ) -> None:
        existing_work_item_id = error.existing_work_item_id
        if existing_work_item_id is None:
            raise ManualDryRunServiceError(
                "manual_work_unavailable",
                "manual work intake is temporarily unavailable",
            )
        work_item = await unit_of_work.works.get(existing_work_item_id)
        run = await unit_of_work.runs.get_by_work_item_id(existing_work_item_id)
        if (
            type(work_item) is not WorkItem
            or type(run) is not Run
            or work_item.source_idempotency_key != envelope.source_key
            or run.work_item_id != work_item.id
            or run.configuration_revision != work_item.configuration_revision
        ):
            raise ManualDryRunServiceError(
                "manual_work_unavailable",
                "manual work intake is temporarily unavailable",
            )
        occurred_at = self._dependencies.utc_now()
        factory = AuditEventFactory(audit_context)
        await unit_of_work.audits.append_many(
            (
                factory.manual_received(
                    work_item,
                    run,
                    attempted_envelope=envelope,
                    manual_attempt_id=manual_attempt_id,
                    disposition="collision",
                    occurred_at=occurred_at,
                ),
                factory.work_idempotency_collision(
                    work_item,
                    run,
                    attempted_envelope=envelope,
                    manual_attempt_id=manual_attempt_id,
                    occurred_at=occurred_at,
                ),
            )
        )

    @staticmethod
    def _require_binding(
        command: ManualDryRunCommand,
        binding: ManualAdmissionBinding,
    ) -> None:
        if type(binding) is not ManualAdmissionBinding:
            raise ManualDryRunServiceError(
                "manual_binding_invalid",
                "manual admission binding is unavailable",
            )
        try:
            binding.__post_init__()
        except (TypeError, ValueError):
            raise ManualDryRunServiceError(
                "manual_binding_invalid",
                "manual admission binding is unavailable",
            ) from None
        if (
            binding.instance_id != command.instance_id
            or binding.demo_scenario_id != command.demo_scenario_id
            or (
                command.campaign_brief_id is not None
                and binding.brief_id != command.campaign_brief_id
            )
            or (
                command.campaign_brief_id is None
                and command.demo_scenario_id is None
                and binding.brief_id is not None
            )
        ):
            raise ManualDryRunServiceError(
                "manual_binding_mismatch",
                "manual admission binding does not match the requested server resource",
            )

    @staticmethod
    def _result(
        command: ManualDryRunCommand,
        binding: ManualAdmissionBinding,
        event_id: str,
        receipt: WorkRunReceiptResult,
    ) -> ManualDryRunResult:
        if type(receipt) is not WorkRunReceiptResult:
            raise ManualDryRunServiceError(
                "manual_receipt_invalid",
                "manual work receipt is unavailable",
            )
        work_item = receipt.work_item
        run = receipt.run
        if (
            type(work_item) is not WorkItem
            or type(run) is not Run
            or type(receipt.disposition) is not WorkRunReceiptDisposition
            or work_item.source != binding.source
            or work_item.event_id != event_id
            or work_item.instance_id != binding.instance_id
            or work_item.trigger_id != binding.trigger_id
            or work_item.workflow_id != binding.workflow_id
            or work_item.mode is not command.mode
            or work_item.brief_id != binding.brief_id
            or work_item.brief_revision != binding.brief_revision
            or work_item.configuration_revision != binding.configuration_revision
            or canonical_json_bytes(work_item.admitted_payload)
            != canonical_json_bytes(command.input_payload)
            or run.work_item_id != work_item.id
            or run.configuration_revision != binding.configuration_revision
            or (
                (receipt.disposition is WorkRunReceiptDisposition.CREATED)
                != (receipt.initial_transition is not None)
            )
        ):
            raise ManualDryRunServiceError(
                "manual_receipt_invalid",
                "manual work receipt is unavailable",
            )
        return ManualDryRunResult(
            work_item=work_item,
            run=run,
            disposition=receipt.disposition,
            event_id=event_id,
            mode=command.mode,
        )

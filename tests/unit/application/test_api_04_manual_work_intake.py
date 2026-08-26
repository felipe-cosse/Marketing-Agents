"""API-04: authorized manual work composes one trusted atomic receipt."""

from __future__ import annotations

from collections import UserList
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, cast

import pytest
from marketing_agents.application.orchestration import OrchestrationDependencies
from marketing_agents.application.policies.runtime_guard import (
    CapabilityPolicy,
    RuntimePolicyGuard,
    RuntimePolicySnapshot,
)
from marketing_agents.application.ports.manual_work import (
    ManualAdmissionBinding,
    ManualAdmissionResolutionError,
)
from marketing_agents.application.ports.unit_of_work import UnitOfWork
from marketing_agents.application.services.idempotent_work_receipt import (
    WorkRunReceiptDisposition,
    WorkRunReceiptResult,
)
from marketing_agents.application.services.incoming_work_validation import (
    ConfiguredIncomingTrigger,
    IncomingWorkValidator,
    ValidatedIncomingWork,
    WorkflowAdmissionDefinition,
)
from marketing_agents.application.services.manual_work_intake import (
    ManualDryRunCommand,
    ManualDryRunService,
    ManualDryRunServiceError,
)
from marketing_agents.application.services.work_admission import WorkIdempotencyError
from marketing_agents.domain.audit import AuditContext, AuditEventDraft
from marketing_agents.domain.data_classification import DataClassification
from marketing_agents.domain.entities import Run, WorkItem
from marketing_agents.domain.enums import RunState, TriggerKind, WorkMode
from marketing_agents.domain.identity import AuthenticatedPrincipal
from marketing_agents.domain.run_lifecycle import initial_received_transition
from marketing_agents.security.digest_key import DigestKey
from marketing_agents.security.redaction import SecretValue

from tests.support.identity import human_principal, service_principal

NOW = datetime(2026, 8, 26, 12, tzinfo=UTC)
CATALOG_HASH = "catalog-sha256-v1:" + ("a" * 64)
INSTANCE_ID = "inst.marketing.content.writer.01"
TEMPLATE_ID = "tpl.test.api-04.manual"
TRIGGER_ID = "trigger.test.api-04.manual"
WORKFLOW_ID = "workflow.test.api-04.manual"
SCHEMA_ID = "urn:marketing-agents:test:api-04:manual-input"
KEY = DigestKey(bytes(range(32)))


@dataclass(frozen=True, slots=True)
class _Template:
    id: str = TEMPLATE_ID
    supported_trigger_types: tuple[str, ...] = ("manual",)


@dataclass(frozen=True, slots=True)
class _Instance:
    id: str = INSTANCE_ID
    template_id: str = TEMPLATE_ID
    enabled: bool = True
    configuration_revision: int = 7


class _Clock:
    def now(self) -> datetime:
        return NOW


class _Ids:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def new(self, namespace: str) -> str:
        self.calls.append(namespace)
        return f"{namespace}.api-04.{len(self.calls):04d}"


class _UnitOfWork:
    def __init__(self) -> None:
        self.enters = 0
        self.exits = 0
        self.commits = 0
        self.audits = _AuditRepository()
        self.works = _ResourceRepository()
        self.runs = _RunResourceRepository()

    async def __aenter__(self) -> _UnitOfWork:
        self.enters += 1
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.exits += 1

    async def commit(self) -> None:
        self.commits += 1


class _AuditRepository:
    def __init__(self) -> None:
        self.batches: list[tuple[AuditEventDraft, ...]] = []

    async def append_many(self, events: tuple[AuditEventDraft, ...]) -> tuple[object, ...]:
        self.batches.append(events)
        return ()

    async def append_global(self, event: AuditEventDraft) -> object:
        self.batches.append((event,))
        return object()


class _ResourceRepository:
    def __init__(self) -> None:
        self.resource: WorkItem | None = None

    async def get(self, _resource_id: str) -> WorkItem | None:
        return self.resource


class _RunResourceRepository:
    def __init__(self) -> None:
        self.resource: Run | None = None

    async def get_by_work_item_id(self, _work_item_id: str) -> Run | None:
        return self.resource


class _UnitOfWorkFactory:
    def __init__(self) -> None:
        self.calls = 0
        self.units: list[_UnitOfWork] = []

    def __call__(self) -> UnitOfWork:
        self.calls += 1
        unit = _UnitOfWork()
        self.units.append(unit)
        return cast(UnitOfWork, unit)


class _Resolver:
    def __init__(
        self,
        binding: ManualAdmissionBinding,
        *,
        error: ManualAdmissionResolutionError | None = None,
    ) -> None:
        self.binding = binding
        self.error = error
        self.calls: list[tuple[UnitOfWork, ManualDryRunCommand]] = []

    async def resolve_in_uow(
        self,
        unit_of_work: UnitOfWork,
        command: ManualDryRunCommand,
    ) -> ManualAdmissionBinding:
        self.calls.append((unit_of_work, command))
        if self.error is not None:
            raise self.error
        return self.binding


class _Receipt:
    def __init__(self, *, mismatch: str | None = None, collision: bool = False) -> None:
        self.mismatch = mismatch
        self.collision = collision
        self.calls: list[tuple[UnitOfWork, ValidatedIncomingWork, object]] = []

    async def receive_in_uow(
        self,
        unit_of_work: UnitOfWork,
        incoming: ValidatedIncomingWork,
        *,
        audit_context: object,
    ) -> WorkRunReceiptResult:
        self.calls.append((unit_of_work, incoming, audit_context))
        envelope = incoming.envelope
        event_id = "manual-event.wrong" if self.mismatch == "event" else envelope.event_id
        work_item = WorkItem(
            id="work.api-04.0001",
            source=envelope.source,
            event_id=event_id,
            instance_id=envelope.instance_id,
            trigger_id=envelope.trigger_id,
            workflow_id=envelope.workflow_id,
            mode=envelope.mode,
            brief_id=envelope.brief_id,
            configuration_revision=envelope.configuration_revision,
            input_digest="b" * 64,
            admission_digest="c" * 64,
            created_at=NOW,
            brief_revision=envelope.brief_revision,
            digest_key_version="admission-hmac-sha256-v1:" + ("d" * 64),
            admitted_payload=(
                {"request": 1} if self.mismatch == "bool_int_payload" else envelope.admitted_payload
            ),
            redacted_input_projection={"request": "safe"},
            input_schema_id=incoming.snapshot.input_schema_id,
            input_schema_hash=incoming.snapshot.input_schema_hash,
            input_classification=DataClassification.INTERNAL,
            input_projection_created_at=NOW,
            input_projection_expires_at=datetime(2026, 9, 2, 12, tzinfo=UTC),
            input_projection_integrity_digest="e" * 64,
        )
        run = Run(
            id="run.api-04.0001",
            work_item_id=work_item.id,
            state=RunState.RECEIVED,
            catalog_hash=CATALOG_HASH,
            configuration_revision=work_item.configuration_revision,
            created_at=NOW,
            updated_at=NOW,
        )
        result = WorkRunReceiptResult(
            work_item=work_item,
            run=run,
            disposition=WorkRunReceiptDisposition.CREATED,
            initial_transition=initial_received_transition(run),
        )
        if self.collision:
            cast(_UnitOfWork, unit_of_work).works.resource = work_item
            cast(_UnitOfWork, unit_of_work).runs.resource = run
            raise WorkIdempotencyError(
                "idempotency_conflict",
                "source idempotency key was already used for different admitted content",
                existing_work_item_id=work_item.id,
            )
        return result


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
            input_max_bytes=10_000,
            output_max_bytes=10_000,
            max_json_depth=10,
            max_content_parts=20,
            max_content_characters=10_000,
            max_model_calls=1,
            max_tool_calls=1,
            rate_window_max_calls=5,
            rate_window_seconds=60,
            step_timeout_seconds=10,
            run_timeout_seconds=60,
        )
    )


def _validator() -> IncomingWorkValidator:
    return IncomingWorkValidator(
        catalog_hash=CATALOG_HASH,
        templates=(_Template(),),
        instances=(_Instance(),),
        input_schemas_by_template={
            TEMPLATE_ID: {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": SCHEMA_ID,
                "type": "object",
                "additionalProperties": False,
                "required": ["request"],
                "properties": {
                    "request": {
                        "type": ["string", "boolean"],
                        "minLength": 1,
                        "maxLength": 100,
                    }
                },
            }
        },
        triggers=(
            ConfiguredIncomingTrigger(
                id=TRIGGER_ID,
                instance_id=INSTANCE_ID,
                kind=TriggerKind.MANUAL,
                source="manual",
                workflow_ids=(WORKFLOW_ID,),
            ),
        ),
        workflows=(
            WorkflowAdmissionDefinition(
                id=WORKFLOW_ID,
                eligible_template_ids=(TEMPLATE_ID,),
                eligible_trigger_kinds=(TriggerKind.MANUAL,),
                allowed_modes=(WorkMode.DRY_RUN, WorkMode.MOCK_EXECUTION),
                input_schema_ids_by_template={TEMPLATE_ID: SCHEMA_ID},
            ),
        ),
        campaign_brief_revisions=(),
        guard=_guard(),
    )


def _binding(**updates: object) -> ManualAdmissionBinding:
    values: dict[str, object] = {
        "instance_id": INSTANCE_ID,
        "source": "manual",
        "trigger_id": TRIGGER_ID,
        "workflow_id": WORKFLOW_ID,
        "configuration_revision": 7,
        "brief_id": None,
        "brief_revision": None,
        "demo_scenario_id": None,
        "validator": _validator(),
    }
    values.update(updates)
    return ManualAdmissionBinding(**values)  # type: ignore[arg-type]


def _command(**updates: object) -> ManualDryRunCommand:
    values: dict[str, object] = {
        "instance_id": INSTANCE_ID,
        "input_payload": {"request": "draft a safe campaign"},
        "mode": WorkMode.DRY_RUN,
        "idempotency_key": SecretValue("manual-retry-key-0001"),
        "campaign_brief_id": None,
        "demo_scenario_id": None,
        "correlation_id": "correlation.api-04.manual.0001",
    }
    values.update(updates)
    return ManualDryRunCommand(**values)  # type: ignore[arg-type]


def _nested_payload(depth: int) -> dict[str, object]:
    root: dict[str, object] = {}
    cursor = root
    for _ in range(depth - 1):
        child: dict[str, object] = {}
        cursor["nested"] = child
        cursor = child
    cursor["request"] = "safe"
    return root


def _user_list_payload(depth: int) -> dict[str, object]:
    value: object = "safe"
    for _ in range(depth - 1):
        value = UserList([value])
    return {"request": value}


def _service(
    *,
    binding: ManualAdmissionBinding | None = None,
    resolver_error: ManualAdmissionResolutionError | None = None,
    receipt: _Receipt | None = None,
) -> tuple[ManualDryRunService, _UnitOfWorkFactory, _Ids, _Resolver, _Receipt]:
    units = _UnitOfWorkFactory()
    ids = _Ids()
    resolver = _Resolver(binding or _binding(), error=resolver_error)
    service = ManualDryRunService(
        OrchestrationDependencies(_Clock(), ids, units),
        KEY,
        resolver,
        current_catalog_hash=CATALOG_HASH,
    )
    receipt_spy = receipt or _Receipt()
    service._receipt = cast(Any, receipt_spy)
    return service, units, ids, resolver, receipt_spy


def _operator() -> AuthenticatedPrincipal:
    return human_principal(
        actor_id="principal.api-04.operator",
        roles=frozenset({"operator"}),
        scopes=frozenset({"manual-work:create"}),
    )


@pytest.mark.asyncio
async def test_api_04_submit_uses_one_uow_trusted_binding_validator_receipt_and_commit() -> None:
    service, units, _ids, resolver, receipt = _service()
    command = _command()

    result = await service.submit(command, _operator())

    assert units.calls == 1
    assert units.units[0].enters == 1
    assert units.units[0].commits == 1
    assert units.units[0].exits == 1
    assert resolver.calls == [(cast(UnitOfWork, units.units[0]), command)]
    assert cast(object, receipt.calls[0][0]) is units.units[0]
    incoming = receipt.calls[0][1]
    assert type(incoming) is ValidatedIncomingWork
    assert incoming.envelope.source == "manual"
    assert incoming.envelope.instance_id == INSTANCE_ID
    assert incoming.envelope.trigger_id == TRIGGER_ID
    assert incoming.envelope.workflow_id == WORKFLOW_ID
    assert incoming.envelope.configuration_revision == 7
    assert incoming.envelope.admitted_payload == command.input_payload
    audit_context = cast(AuditContext, receipt.calls[0][2])
    assert audit_context.binds_authenticated_user(
        actor_id="principal.api-04.operator",
        authentication_method="bearer",
        correlation_id=command.correlation_id,
    )
    assert result.disposition is WorkRunReceiptDisposition.CREATED
    assert result.event_id == incoming.envelope.event_id
    assert result.mode is WorkMode.DRY_RUN
    assert result.work_item.id == "work.api-04.0001"
    assert result.run.id == "run.api-04.0001"
    audit_batch = units.units[0].audits.batches[0]
    assert tuple(event.event_type for event in audit_batch) == (
        "ingress.manual_received",
        "work.created",
    )
    assert tuple(event.safe_metadata.values["receipt_disposition"] for event in audit_batch) == (
        "created",
        "created",
    )
    assert all(event.run_id == result.run.id for event in audit_batch)
    serialized_audit = repr(audit_batch)
    assert "manual-retry-key-0001" not in serialized_audit
    assert "draft a safe campaign" not in serialized_audit


@pytest.mark.asyncio
async def test_api_04_keyed_event_is_stable_non_reversible_and_absent_key_is_fresh() -> None:
    service, _units, ids, _resolver, receipt = _service()
    operator = _operator()
    key = "manual-retry-key-sensitive-0001"

    first = await service.submit(_command(idempotency_key=SecretValue(key)), operator)
    second = await service.submit(_command(idempotency_key=SecretValue(key)), operator)
    third = await service.submit(
        _command(idempotency_key=SecretValue("manual-retry-key-sensitive-0002")),
        operator,
    )
    fresh_a = await service.submit(_command(idempotency_key=None), operator)
    fresh_b = await service.submit(_command(idempotency_key=None), operator)

    assert first.event_id == second.event_id
    assert first.event_id.startswith("manual-event-hmac-sha256-v1:")
    assert key not in first.event_id
    assert third.event_id != first.event_id
    assert fresh_a.event_id != fresh_b.event_id
    assert ids.calls == [
        "manual-ingress",
        "manual-ingress",
        "manual-ingress",
        "manual-event",
        "manual-ingress",
        "manual-event",
        "manual-ingress",
    ]
    assert len(receipt.calls) == 5
    assert key not in repr(_command(idempotency_key=SecretValue(key)))


@pytest.mark.asyncio
async def test_api_04_authorization_rejects_before_uow_or_resolver_lookup() -> None:
    command = _command()
    principals = (
        service_principal(roles=frozenset({"operator"})),
        human_principal(roles=frozenset({"viewer"})),
    )
    expected_codes = (
        "manual_work_human_required",
        "manual_work_operator_role_missing",
    )
    for principal, expected_code in zip(principals, expected_codes, strict=True):
        service, units, ids, resolver, receipt = _service()
        with pytest.raises(ManualDryRunServiceError) as rejected:
            await service.submit(command, principal)
        assert rejected.value.code == expected_code
        assert units.calls == 0
        assert ids.calls == []
        assert resolver.calls == []
        assert receipt.calls == []

    tampered = _operator()
    object.__setattr__(tampered, "roles", frozenset({"operator", "local_admin"}))
    service, units, _ids, resolver, _receipt = _service()
    with pytest.raises(ManualDryRunServiceError) as rejected:
        await service.submit(command, tampered)
    assert rejected.value.code == "manual_work_human_required"
    assert units.calls == 0
    assert resolver.calls == []


def test_api_04_command_is_strict_frozen_redacted_and_has_no_routing_authority() -> None:
    mutable = {"request": "safe", "nested": {"items": ["one"]}}
    command = _command(input_payload=mutable)
    mutable["request"] = "changed"
    cast(dict[str, object], mutable["nested"])["items"] = ["changed"]

    assert command.input_payload == {
        "request": "safe",
        "nested": {"items": ("one",)},
    }
    assert type(cast(object, command.input_payload)) is MappingProxyType
    assert "input_payload" not in repr(command)
    assert "manual-retry-key-0001" not in repr(command)
    punctuation_key = SecretValue("opaque:+?=%#@![]{}retry")
    assert _command(idempotency_key=punctuation_key).idempotency_key is punctuation_key
    assert not {
        "event_id",
        "workflow_id",
        "trigger_id",
        "configuration_revision",
        "actor_id",
    }.intersection(command.__dataclass_fields__)

    for updates, code in (
        ({"mode": "dry_run"}, "manual_work_command_invalid"),
        ({"idempotency_key": "raw-key-not-secret"}, "manual_idempotency_key_invalid"),
        ({"idempotency_key": SecretValue("short")}, "manual_idempotency_key_invalid"),
        ({"idempotency_key": SecretValue(" invalid-key")}, "manual_idempotency_key_invalid"),
        (
            {"idempotency_key": SecretValue("non-ascii-é-key")},
            "manual_idempotency_key_invalid",
        ),
        (
            {"idempotency_key": SecretValue("control-\t-key")},
            "manual_idempotency_key_invalid",
        ),
        ({"input_payload": {"invalid": float("nan")}}, "manual_work_command_invalid"),
    ):
        with pytest.raises(ManualDryRunServiceError) as invalid:
            _command(**updates)
        assert invalid.value.code == code


def test_api_04_command_depth_guard_is_iterative_and_stable() -> None:
    accepted = _command(input_payload=_nested_payload(64))
    assert accepted.input_payload["nested"] is not None

    for depth in (65, 1_100):
        with pytest.raises(ManualDryRunServiceError) as rejected:
            _command(input_payload=_nested_payload(depth))
        assert rejected.value.code == "manual_work_command_invalid"
        assert str(rejected.value) == "manual work command is invalid"

    with pytest.raises(ManualDryRunServiceError) as user_list_rejected:
        _command(input_payload=_user_list_payload(1_100))
    assert user_list_rejected.value.code == "manual_work_command_invalid"


@pytest.mark.asyncio
async def test_api_04_mismatched_binding_and_receipt_fail_before_commit() -> None:
    service, units, _ids, _resolver, _receipt = _service(
        binding=_binding(instance_id="inst.marketing.content.writer.02")
    )
    with pytest.raises(ManualDryRunServiceError) as mismatch:
        await service.submit(_command(), _operator())
    assert mismatch.value.code == "manual_binding_mismatch"
    assert units.units[0].commits == 0

    service, units, _ids, _resolver, _receipt = _service(receipt=_Receipt(mismatch="event"))
    with pytest.raises(ManualDryRunServiceError) as invalid_receipt:
        await service.submit(_command(), _operator())
    assert invalid_receipt.value.code == "manual_receipt_invalid"
    assert units.units[0].commits == 0

    service, units, _ids, _resolver, _receipt = _service(
        receipt=_Receipt(mismatch="bool_int_payload")
    )
    with pytest.raises(ManualDryRunServiceError) as invalid_payload_receipt:
        await service.submit(_command(input_payload={"request": True}), _operator())
    assert invalid_payload_receipt.value.code == "manual_receipt_invalid"
    assert units.units[0].commits == 0

    command = _command()
    object.__setattr__(command, "mode", "dry_run")
    service, units, _ids, resolver, _receipt = _service()
    with pytest.raises(ManualDryRunServiceError) as invalid_command:
        await service.submit(command, _operator())
    assert invalid_command.value.code == "manual_work_command_invalid"
    assert units.calls == 0
    assert resolver.calls == []


@pytest.mark.asyncio
async def test_api_04_resolver_errors_are_fixed_safe_and_unknown_fail_closed() -> None:
    for resolution, expected_code, expected_message in (
        (
            ManualAdmissionResolutionError("instance_unknown", "secret.canary"),
            "instance_unknown",
            "agent instance is not registered",
        ),
        (
            ManualAdmissionResolutionError("untrusted_code", "secret.canary"),
            "manual_work_unavailable",
            "manual work intake is temporarily unavailable",
        ),
    ):
        service, units, _ids, _resolver, _receipt = _service(resolver_error=resolution)
        with pytest.raises(ManualDryRunServiceError) as rejected:
            await service.submit(_command(), _operator())
        assert rejected.value.code == expected_code
        assert str(rejected.value) == expected_message
        assert "secret.canary" not in str(rejected.value)
        assert units.units[0].commits == 0


@pytest.mark.asyncio
async def test_api_04_schema_rejection_preserves_only_safe_input_pointer() -> None:
    service, units, _ids, _resolver, _receipt = _service()

    with pytest.raises(ManualDryRunServiceError) as rejected:
        await service.submit(
            _command(input_payload={"request": 42}),
            _operator(),
        )

    assert rejected.value.code == "input_schema_invalid"
    assert rejected.value.pointer == "/input/request"
    assert units.units[0].commits == 1
    rejection_audit = units.units[0].audits.batches[0][0]
    assert rejection_audit.event_type == "ingress.schema_rejected"
    assert rejection_audit.run_id is None
    assert rejection_audit.reason_code == "schema_rejected"
    assert rejection_audit.safe_metadata.values["rejection_code"] == "schema_rejected"
    assert set(rejection_audit.safe_metadata.values) == {
        "configuration_revision",
        "instance_id",
        "manual_attempt_id",
        "mode",
        "rejection_code",
        "trigger_id",
        "workflow_id",
    }
    assert (
        ManualDryRunServiceError(
            "input_schema_invalid",
            "safe",
            pointer="/input/secret~1canary",
        ).pointer
        is None
    )


@pytest.mark.asyncio
async def test_api_04_collision_commits_only_a_redacted_authoritative_witness() -> None:
    raw_key = "collision-secret-key-api-04"
    payload_canary = "collision-payload-canary-api-04"
    service, units, _ids, _resolver, _receipt = _service(receipt=_Receipt(collision=True))

    with pytest.raises(ManualDryRunServiceError) as rejected:
        await service.submit(
            _command(
                idempotency_key=SecretValue(raw_key),
                input_payload={"request": payload_canary},
            ),
            _operator(),
        )

    assert rejected.value.code == "idempotency_conflict"
    assert units.units[0].commits == 1
    audit_batch = units.units[0].audits.batches[0]
    assert tuple(event.event_type for event in audit_batch) == (
        "ingress.manual_received",
        "work.idempotency_collision",
    )
    assert audit_batch[1].outcome.value == "rejected"
    assert audit_batch[1].reason_code == "idempotency_conflict"
    assert audit_batch[1].mutation_version is None
    serialized_audit = repr(audit_batch)
    assert raw_key not in serialized_audit
    assert payload_canary not in serialized_audit

"""RUN-06: durable schema-derived redacted admitted-input projections."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from marketing_agents.application.orchestration import OrchestrationDependencies
from marketing_agents.application.ports.repositories import (
    AuditRepository,
    RunRepository,
    WorkRepository,
)
from marketing_agents.application.services import (
    AdmissionDisposition,
    ConfiguredIncomingTrigger,
    IncomingWorkValidationError,
    IncomingWorkValidator,
    WorkAdmissionService,
    WorkflowAdmissionDefinition,
    WorkIdempotencyError,
)
from marketing_agents.domain.admission import AdmissionEnvelope
from marketing_agents.domain.data_classification import DataClassification
from marketing_agents.domain.enums import TriggerKind, WorkMode
from marketing_agents.infrastructure.db import (
    Base,
    DatabaseRuntime,
    SQLAlchemyRepositoryFactories,
    SQLAlchemyUnitOfWorkFactory,
    create_database_runtime,
)
from marketing_agents.infrastructure.db.models import WorkItemRecord
from marketing_agents.infrastructure.db.repositories import SQLAlchemyWorkRepository
from marketing_agents.security.digest_key import DigestKey
from marketing_agents.security.redaction import REDACTED, redact_json_pointers
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

NOW = datetime(2026, 8, 25, 12, tzinfo=UTC)
CATALOG_HASH = "catalog-sha256-v1:" + ("a" * 64)
TEMPLATE_ID = "tpl.test.run-06.input-redaction"
INSTANCE_ID = "inst.test.run-06.input-redaction.01"
TRIGGER_ID = "trigger.test.run-06.manual"
WORKFLOW_ID = "workflow.test.run-06.v1"
SCHEMA_ID = "urn:marketing-agents:test:run-06:input"
PII_CANARY = "run06-person@example.invalid"
SECRET_CANARY = "run06-secret-token-canary"
SENSITIVE_CANARY = "run06-sensitive-application-canary"


@dataclass(frozen=True, slots=True)
class TemplateStub:
    id: str = TEMPLATE_ID
    supported_trigger_types: tuple[str, ...] = ("manual",)


@dataclass(frozen=True, slots=True)
class InstanceStub:
    id: str = INSTANCE_ID
    template_id: str = TEMPLATE_ID
    enabled: bool = True
    configuration_revision: int = 1


class SchemaGuard:
    def validate_input(self, payload: Any, schema: Mapping[str, Any]) -> None:
        Draft202012Validator(schema).validate(payload)


class SequenceClock:
    def __init__(self, *values: datetime) -> None:
        self._values = iter(values)

    def now(self) -> datetime:
        return next(self._values)


class IncrementingIds:
    def __init__(self) -> None:
        self._next = 0

    def new(self, namespace: str) -> str:
        self._next += 1
        return f"{namespace}.run-06.{self._next:04d}"


def _schema(schema_id: str = SCHEMA_ID) -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": schema_id,
        "type": "object",
        "additionalProperties": False,
        "required": ["request_id", "contact", "members", "submission"],
        "properties": {
            "request_id": {"type": "string"},
            "contact": {
                "type": "object",
                "additionalProperties": False,
                "required": ["email", "display"],
                "properties": {
                    "email": {
                        "type": "string",
                        "x-data-classification": "personal",
                    },
                    "display": {"type": "string"},
                },
            },
            "members": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["full_name", "status"],
                    "properties": {
                        "full_name": {"type": "string", "x-sensitive": True},
                        "status": {"type": "string"},
                    },
                },
            },
            "submission": {
                "type": "object",
                "additionalProperties": False,
                "required": ["application_text", "label"],
                "properties": {
                    "application_text": {"type": "string", "x-sensitive": True},
                    "label": {"type": "string"},
                },
            },
        },
    }


def _payload() -> dict[str, Any]:
    return {
        "request_id": "request-run-06-1",
        "contact": {"email": PII_CANARY, "display": "Useful label"},
        "members": [
            {"full_name": "Run Six Canary Person", "status": "active"},
            {"full_name": "Second Canary Person", "status": "pending"},
        ],
        "submission": {"application_text": SENSITIVE_CANARY, "label": "mock binding"},
    }


def _envelope() -> AdmissionEnvelope:
    return AdmissionEnvelope(
        source="manual",
        event_id="event.run-06.0001",
        instance_id=INSTANCE_ID,
        trigger_id=TRIGGER_ID,
        workflow_id=WORKFLOW_ID,
        mode=WorkMode.MOCK_EXECUTION,
        brief_id=None,
        brief_revision=None,
        configuration_revision=1,
        admitted_payload=_payload(),
    )


def _validator(schema_id: str = SCHEMA_ID) -> IncomingWorkValidator:
    return IncomingWorkValidator(
        catalog_hash=CATALOG_HASH,
        templates=(TemplateStub(),),
        instances=(InstanceStub(),),
        input_schemas_by_template={TEMPLATE_ID: _schema(schema_id)},
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
                allowed_modes=(WorkMode.MOCK_EXECUTION,),
                input_schema_ids_by_template={TEMPLATE_ID: schema_id},
            ),
        ),
        campaign_brief_revisions=(),
        guard=SchemaGuard(),
    )


def _unused_run_repository(_session: AsyncSession) -> RunRepository:
    return cast(RunRepository, object())


def _unused_audit_repository(_session: AsyncSession) -> AuditRepository:
    return cast(AuditRepository, object())


async def _runtime(path: Path) -> DatabaseRuntime:
    runtime = create_database_runtime(f"sqlite+aiosqlite:///{path}")
    async with runtime.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return runtime


def _service(
    runtime: DatabaseRuntime,
    *,
    clock: SequenceClock | None = None,
    work_factory: Callable[[AsyncSession], WorkRepository] = SQLAlchemyWorkRepository,
) -> WorkAdmissionService:
    unit_of_work_factory = SQLAlchemyUnitOfWorkFactory(
        runtime.session_factory,
        SQLAlchemyRepositoryFactories(
            works=work_factory,
            runs=_unused_run_repository,
            audits=_unused_audit_repository,
        ),
    )
    return WorkAdmissionService(
        OrchestrationDependencies(
            clock=clock or SequenceClock(NOW),
            ids=IncrementingIds(),
            unit_of_work_factory=unit_of_work_factory,
        ),
        DigestKey(bytes(range(32))),
    )


async def _count(runtime: DatabaseRuntime) -> int:
    async with runtime.session_factory() as session:
        return int((await session.execute(select(func.count(WorkItemRecord.id)))).scalar_one())


def test_run_06_pointer_redaction_copies_nested_arrays_wildcards_and_escaped_keys() -> None:
    payload = {
        "records": [
            {"attributes": {"author_ref": "author-canary-1"}, "note": "first"},
            {"attributes": {"author_ref": "author-canary-2"}, "note": "second"},
        ],
        "by_id": {
            "first": {"external_ref": "mapping-canary-1"},
            "second": {"external_ref": "mapping-canary-2"},
        },
        "metadata": {"a/b": {"literal~field": "escaped-canary"}},
        "contact_email": PII_CANARY,
        "credentials": {"token": SECRET_CANARY},
        "classified": "classified-canary",
    }
    schema = {
        "type": "object",
        "properties": {
            "classified": {"type": "string", "x-data-classification": "sensitive"},
        },
    }

    projected = redact_json_pointers(
        payload,
        (
            "/records/*/attributes/author_ref",
            "/by_id/*/external_ref",
            "/records/0/note",
            "/metadata/a~1b/literal~0field",
            "/credentials/token",
            "/missing/optional",
        ),
        schema=schema,
    )

    assert payload["records"][0]["attributes"]["author_ref"] == "author-canary-1"
    assert projected["records"] == [
        {"attributes": {"author_ref": REDACTED}, "note": REDACTED},
        {"attributes": {"author_ref": REDACTED}, "note": "second"},
    ]
    assert projected["by_id"] == {
        "first": {"external_ref": REDACTED},
        "second": {"external_ref": REDACTED},
    }
    assert projected["metadata"]["a/b"]["literal~field"] == REDACTED
    assert projected["contact_email"] == REDACTED
    assert projected["credentials"] == REDACTED
    assert projected["classified"] == REDACTED
    rendered = json.dumps(projected)
    for canary in (
        PII_CANARY,
        "author-canary-1",
        "author-canary-2",
        "mapping-canary-1",
        "mapping-canary-2",
        "escaped-canary",
    ):
        assert canary not in rendered

    with pytest.raises(ValueError, match="RFC 6901"):
        redact_json_pointers(payload, ("records/*",))
    with pytest.raises(ValueError, match="traverses a scalar"):
        redact_json_pointers(payload, ("/records/0/note/value",))


@pytest.mark.asyncio
async def test_run_06_projection_is_schema_bound_redacted_immutable_and_replayed_exactly(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path / "projection.db")
    service = _service(runtime, clock=SequenceClock(NOW, NOW + timedelta(days=1)))
    validated = _validator().validate(_envelope())
    try:
        created = await service.admit(validated)
        replayed = await service.admit(_validator().validate(_envelope()))

        work = created.work_item
        assert created.disposition is AdmissionDisposition.CREATED
        assert replayed.disposition is AdmissionDisposition.REPLAYED
        assert replayed.work_item == work
        assert replayed.work_item.id == work.id
        assert work.input_schema_id == validated.snapshot.input_schema_id
        assert work.input_schema_hash == validated.snapshot.input_schema_hash
        assert work.input_classification is DataClassification.SENSITIVE
        assert work.input_projection_created_at == NOW
        assert work.input_projection_expires_at == NOW + timedelta(days=7)
        assert len(work.input_projection_integrity_digest) == 64
        assert work.redacted_input_projection["contact"] == {
            "email": REDACTED,
            "display": "Useful label",
        }
        assert work.redacted_input_projection["members"] == (
            {"full_name": REDACTED, "status": "active"},
            {"full_name": REDACTED, "status": "pending"},
        )
        assert work.redacted_input_projection["submission"] == {
            "application_text": REDACTED,
            "label": "mock binding",
        }
        rendered = repr(work) + json.dumps(
            json.loads(json.dumps(work.redacted_input_projection, default=dict))
        )
        assert PII_CANARY not in rendered
        assert SENSITIVE_CANARY not in rendered
        with pytest.raises(TypeError):
            work.redacted_input_projection["contact"]["email"] = "changed"  # type: ignore[index]

        async with runtime.session_factory() as session:
            record = await session.get(WorkItemRecord, work.id)
            assert record is not None
            assert record.admitted_payload["contact"]["email"] == PII_CANARY
            persisted_projection = json.dumps(record.redacted_input_projection)
            assert PII_CANARY not in persisted_projection
            assert SENSITIVE_CANARY not in persisted_projection
        assert await _count(runtime) == 1
    finally:
        await runtime.dispose()


@pytest.mark.parametrize(
    ("field_name", "tampered_value"),
    [
        ("redacted_input_projection", {"request_id": "tampered"}),
        ("input_schema_hash", "schema-sha256-v1:" + ("b" * 64)),
        ("input_classification", DataClassification.PERSONAL.value),
        ("input_projection_expires_at", NOW + timedelta(days=8)),
        ("input_projection_integrity_digest", "f" * 64),
    ],
)
@pytest.mark.asyncio
async def test_run_06_projection_metadata_tamper_fails_closed(
    tmp_path: Path,
    field_name: str,
    tampered_value: object,
) -> None:
    runtime = await _runtime(tmp_path / f"tamper-{field_name}.db")
    service = _service(runtime, clock=SequenceClock(NOW, NOW + timedelta(minutes=1)))
    try:
        created = await service.admit(_validator().validate(_envelope()))
        async with runtime.session_factory() as session:
            record = await session.get(WorkItemRecord, created.work_item.id)
            assert record is not None
            setattr(record, field_name, tampered_value)
            await session.commit()

        with pytest.raises(WorkIdempotencyError) as rejected:
            await service.admit(_validator().validate(_envelope()))
        assert rejected.value.code == "input_projection_integrity_mismatch"
        assert PII_CANARY not in repr(rejected.value)
        assert SECRET_CANARY not in repr(rejected.value)
        assert await _count(runtime) == 1
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_run_06_semantic_replay_rejects_wrong_schema_identity(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path / "schema-identity.db")
    service = _service(runtime, clock=SequenceClock(NOW, NOW + timedelta(minutes=1)))
    try:
        created = await service.admit(_validator().validate(_envelope()))
        changed_schema_id = "urn:marketing-agents:test:run-06:changed-input"

        with pytest.raises(WorkIdempotencyError) as rejected:
            await service.admit(_validator(changed_schema_id).validate(_envelope()))

        assert rejected.value.code == "input_projection_schema_mismatch"
        assert rejected.value.existing_work_item_id == created.work_item.id
        assert await _count(runtime) == 1
    finally:
        await runtime.dispose()


def test_run_06_validator_projection_tamper_and_secret_classification_fail_closed() -> None:
    validated = _validator().validate(_envelope())
    object.__setattr__(validated, "redacted_input_projection", {"leak": PII_CANARY})
    with pytest.raises(IncomingWorkValidationError) as tampered:
        # The private verifier is exercised through the only public admission boundary
        # in integration tests; invoking validation integrity here stays side-effect free.
        from marketing_agents.application.services.incoming_work_validation import (
            _validated_parts,
        )

        _validated_parts(validated)
    assert tampered.value.code == "incoming_work_not_validated"

    schema = _schema()
    schema["properties"]["submission"]["properties"]["application_text"] = {
        "type": "string",
        "x-data-classification": "secret",
    }
    secret_validator = IncomingWorkValidator(
        catalog_hash=CATALOG_HASH,
        templates=(TemplateStub(),),
        instances=(InstanceStub(),),
        input_schemas_by_template={TEMPLATE_ID: schema},
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
                allowed_modes=(WorkMode.MOCK_EXECUTION,),
                input_schema_ids_by_template={TEMPLATE_ID: SCHEMA_ID},
            ),
        ),
        campaign_brief_revisions=(),
        guard=SchemaGuard(),
    )
    with pytest.raises(IncomingWorkValidationError) as secret:
        secret_payload = _payload()
        secret_payload["submission"]["application_text"] = SECRET_CANARY
        secret_validator.validate(replace(_envelope(), admitted_payload=secret_payload))
    assert secret.value.code == "input_secret_not_retainable"
    assert SECRET_CANARY not in repr(secret.value)

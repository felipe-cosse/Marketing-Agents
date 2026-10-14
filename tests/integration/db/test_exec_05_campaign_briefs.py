"""EXEC-05 standalone briefs preserve input, restrict projections and survive restart."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from marketing_agents.api import create_app
from marketing_agents.application.orchestration.dependencies import OrchestrationDependencies
from marketing_agents.application.services.campaign_briefs import (
    CampaignBriefError,
    CampaignBriefService,
    CreateCampaignBriefCommand,
)
from marketing_agents.config import Settings
from marketing_agents.domain.audit import AuditEventDraft
from marketing_agents.domain.retention import RetentionPolicy
from marketing_agents.infrastructure.db import (
    SQLAlchemyAuditRepository,
    SQLAlchemyManualAdmissionUnitOfWorkFactory,
    SQLAlchemyRepositoryFactories,
    SQLAlchemyRunRepository,
    SQLAlchemyWorkRepository,
    create_database_runtime,
)
from marketing_agents.infrastructure.db.migrations import upgrade_database
from marketing_agents.infrastructure.db.models.audit import AuditEventRecord
from marketing_agents.infrastructure.db.models.campaign_brief import CampaignBriefRecord
from marketing_agents.infrastructure.db.repositories.campaign_briefs import (
    SQLAlchemyCampaignBriefRepository,
)
from marketing_agents.infrastructure.db.schema import schema_matches_metadata
from marketing_agents.security.digest_key import DigestKey
from marketing_agents.security.redaction import SecretValue
from sqlalchemy import func, inspect, select, update
from sqlalchemy.exc import IntegrityError

from tests.support.api import browser_request
from tests.support.identity import StaticIdentityProvider, human_principal, service_principal

NOW = datetime(2026, 10, 14, tzinfo=UTC)
KEY = DigestKey(bytes(range(32)))
OPERATOR = human_principal(roles=frozenset({"operator"}))
VIEWER = human_principal(roles=frozenset({"viewer"}))
OBJECTIVE = "objective-canary-private"
EMAIL = "canary-private@example.invalid"
REF = "source-canary-private"


class Clock:
    def __init__(self, now=NOW):
        self.value = now

    def now(self):
        return self.value


class Ids:
    def new(self, namespace):
        return f"{namespace}.exec05.brief"


class FailingAudit(SQLAlchemyAuditRepository):
    async def append_global_many(self, events: tuple[AuditEventDraft, ...]):
        await super().append_global_many(events)
        raise RuntimeError("injected safe audit failure")


def _service(database, *, clock=None, key=KEY, audit=SQLAlchemyAuditRepository, retention=None):
    factories = SQLAlchemyRepositoryFactories(
        works=SQLAlchemyWorkRepository,
        runs=SQLAlchemyRunRepository,
        audits=audit,
        campaign_briefs=SQLAlchemyCampaignBriefRepository,
    )
    dependencies = OrchestrationDependencies(
        clock or Clock(),
        Ids(),
        SQLAlchemyManualAdmissionUnitOfWorkFactory(database.session_factory, factories),
    )
    return CampaignBriefService(dependencies, key, retention)


def _command(**changes):
    fields = dict(
        title="Launch brief",
        objective=OBJECTIVE,
        constraints={"channel": "email", "contact_email": EMAIL},
        source_refs=(REF,),
        idempotency_key=SecretValue("retry-exec05-brief"),
        correlation_id="corr.exec05.brief",
    )
    return CreateCampaignBriefCommand(**(fields | changes))


def _body(**changes):
    return (
        dict(
            title="Launch brief",
            objective=OBJECTIVE,
            constraints={"channel": "email", "contact_email": EMAIL},
            sourceRefs=[REF],
        )
        | changes
    )


async def _database(tmp_path):
    database = create_database_runtime(f"sqlite+aiosqlite:///{tmp_path / 'briefs.db'}")
    await upgrade_database(database)
    return database


async def _counts(database):
    async with database.engine.connect() as connection:
        return tuple(
            [
                await connection.scalar(select(func.count()).select_from(model))
                for model in (CampaignBriefRecord, AuditEventRecord)
            ]
        )


@pytest.mark.asyncio
async def test_exec05_brief_create_read_retry_conflict_and_restart(tmp_path):
    database = await _database(tmp_path)
    try:
        service = _service(database)
        created = await service.create(principal=OPERATOR, command=_command())
        assert created.disposition == "created" and created.resource.revision == 1
        view = await service.read(principal=VIEWER, brief_id=created.resource.id)
        assert view == created.resource
        assert view.details["title"] == "Launch brief"
        assert view.details["constraints"]["channel"] == "email"
        for canary in (OBJECTIVE, EMAIL, REF):
            assert canary not in repr(view) and canary not in str(dict(view.details))
            assert canary not in repr(_command())
        async with database.session_factory() as session:
            record = await session.get(CampaignBriefRecord, (view.id, 1))
            document = json.loads(record.snapshot_json)
            assert document["content"]["objective"] == OBJECTIVE
            assert document["content"]["constraints"]["contact_email"] == EMAIL
            assert document["content"]["source_refs"] == [REF]
            audit = (await session.scalars(select(AuditEventRecord))).one()
            assert audit.safe_metadata == {"new_revision": 1}
            assert audit.aggregate_id == view.id and audit.run_id is None
        replay = await service.create(principal=OPERATOR, command=_command())
        assert replay.disposition == "replayed" and replay.resource == view
        with pytest.raises(CampaignBriefError) as caught:
            # The redacted projection is identical; original body equality still matters.
            await service.create(
                principal=OPERATOR, command=_command(objective="other private text")
            )
        assert caught.value.code == "campaign_brief_idempotency_conflict"
        assert await _counts(database) == (1, 1)
        identifier = view.id
    finally:
        await database.dispose()
    restarted = create_database_runtime(f"sqlite+aiosqlite:///{tmp_path / 'briefs.db'}")
    try:
        assert await upgrade_database(restarted) == "0010"
        service = _service(restarted)
        assert (await service.read(principal=VIEWER, brief_id=identifier)).id == identifier
        replay = await service.create(principal=OPERATOR, command=_command())
        assert replay.disposition == "replayed" and replay.resource.id == identifier
        assert await _counts(restarted) == (1, 1)
    finally:
        await restarted.dispose()


@pytest.mark.asyncio
async def test_exec05_brief_concurrent_retry_and_configured_expired_projection(tmp_path):
    database = await _database(tmp_path)
    clock = Clock()
    service = _service(database, clock=clock, retention=RetentionPolicy(admitted_payload_days=2))
    try:
        results = await asyncio.gather(
            *(service.create(principal=OPERATOR, command=_command()) for _ in range(4))
        )
        assert sorted(result.disposition for result in results) == [
            "created",
            "replayed",
            "replayed",
            "replayed",
        ]
        assert len({result.resource.id for result in results}) == 1
        clock.value += timedelta(days=2)
        view = await service.read(principal=VIEWER, brief_id=results[0].resource.id)
        assert view.expired and view.details is None
        assert view.expires_at == NOW + timedelta(days=2)
        replay = await service.create(principal=OPERATOR, command=_command())
        assert replay.resource == view  # Replay never refreshes expiry or exposes old details.
        async with database.session_factory() as session:
            record = await session.get(CampaignBriefRecord, (view.id, 1))
            assert OBJECTIVE in record.snapshot_json  # Projection expiry is not physical erasure.
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_exec05_brief_secret_authentication_and_audit_failure_leave_no_rows(tmp_path):
    database = await _database(tmp_path)
    try:
        service = _service(database)
        with pytest.raises(CampaignBriefError) as caught:
            await service.create(
                principal=OPERATOR, command=_command(constraints={"api_key": "secret-canary"})
            )
        assert caught.value.code == "campaign_brief_secret_not_retainable"
        for principal in (VIEWER, service_principal(), object()):
            with pytest.raises(CampaignBriefError) as caught:
                await service.create(principal=principal, command=_command())
            assert caught.value.code == "campaign_brief_forbidden"
        for principal in (
            human_principal(roles=frozenset({"unrelated"})),
            service_principal(),
            object(),
        ):
            with pytest.raises(CampaignBriefError) as caught:
                await service.read(principal=principal, brief_id="not-even-a-brief")
            assert caught.value.code == "campaign_brief_forbidden"
        with pytest.raises(CampaignBriefError) as caught:
            await _service(database, audit=FailingAudit).create(
                principal=OPERATOR, command=_command()
            )
        assert caught.value.code == "campaign_brief_unavailable"
        assert await _counts(database) == (0, 0)
    finally:
        await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [("snapshot_hash", "a" * 64), ("title", "tampered label"), ("snapshot_json", "{}")],
)
async def test_exec05_brief_tampering_and_wrong_key_fail_closed(tmp_path, field, value):
    database = await _database(tmp_path)
    try:
        service = _service(database)
        created = await service.create(principal=OPERATOR, command=_command())
        with pytest.raises(CampaignBriefError) as caught:
            await _service(database, key=DigestKey(b"x" * 32)).read(
                principal=VIEWER, brief_id=created.resource.id
            )
        assert caught.value.code == "campaign_brief_unavailable"
        async with database.engine.begin() as connection:
            await connection.execute(update(CampaignBriefRecord).values(**{field: value}))
        with pytest.raises(CampaignBriefError) as caught:
            await service.read(principal=VIEWER, brief_id=created.resource.id)
        assert caught.value.code == "campaign_brief_unavailable"
        with pytest.raises(CampaignBriefError):
            await service.create(principal=OPERATOR, command=_command())
        assert await _counts(database) == (1, 1)
    finally:
        await database.dispose()


def _app(service, principal=OPERATOR):
    return create_app(
        Settings(),
        identity_provider=StaticIdentityProvider(principal),
        campaign_brief_service=service,
    )


@pytest.mark.asyncio
async def test_exec05_brief_http_create_get_retry_headers_and_safe_projection(tmp_path):
    database = await _database(tmp_path)
    clock = Clock()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=_app(_service(database, clock=clock))),
            base_url="http://testserver",
        ) as client:
            response = await browser_request(
                client,
                "POST",
                "/api/v1/campaign-briefs",
                json=_body(),
                headers={"Idempotency-Key": "exec05-http-retry"},
            )
            assert response.status_code == 201, response.text
            assert response.headers["cache-control"] == "no-store"
            identifier = response.json()["resource"]["id"]
            read = await client.get(f"/api/v1/campaign-briefs/{identifier}")
            assert read.status_code == 200 and read.json() == response.json()["resource"]
            assert read.headers["cache-control"] == "no-store"
            assert read.json()["details"]["sourceRefs"] == "[REDACTED]"
            for canary in (OBJECTIVE, EMAIL, REF):
                assert canary not in read.text and canary not in response.text
            retry = await browser_request(
                client,
                "POST",
                "/api/v1/campaign-briefs",
                json=_body(),
                headers={"Idempotency-Key": "exec05-http-retry"},
            )
            assert retry.status_code == 200 and retry.json()["disposition"] == "replayed"
            conflict = await browser_request(
                client,
                "POST",
                "/api/v1/campaign-briefs",
                json=_body(objective="changed"),
                headers={"Idempotency-Key": "exec05-http-retry"},
            )
            assert conflict.status_code == 409
            missing = await browser_request(client, "POST", "/api/v1/campaign-briefs", json=_body())
            assert missing.status_code == 400
            clock.value += timedelta(days=7)
            expired = await client.get(f"/api/v1/campaign-briefs/{identifier}")
            assert expired.status_code == 200
            assert expired.json()["expired"] is True and expired.json()["details"] is None
            assert expired.headers["cache-control"] == "no-store"
    finally:
        await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body,expected_status",
    [
        (b'{"title":"x","title":"y"}', 400),
        (b'{"constraints":{"x":NaN}}', 400),
        (b'{"x":"\xff"}', 400),
        (b'{"title":"x"}' + b" " * 65_536, 413),
        (b'{"constraints":' + b"[" * 20 + b"0" + b"]" * 20 + b"}", 400),
        (b'{"title":"x"}', 422),
    ],
)
async def test_exec05_brief_http_strict_json_never_persists(tmp_path, body, expected_status):
    database = await _database(tmp_path)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=_app(_service(database))), base_url="http://testserver"
        ) as client:
            response = await browser_request(
                client,
                "POST",
                "/api/v1/campaign-briefs",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "Idempotency-Key": "exec05-http-strict",
                },
            )
            assert response.status_code == expected_status
            assert response.headers["content-type"] == "application/problem+json"
            assert response.json()["status"] == expected_status
            assert response.headers["cache-control"] == "no-store"
            assert await _counts(database) == (0, 0)
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_exec05_brief_http_viewer_cannot_create_and_csrf_is_required(tmp_path):
    database = await _database(tmp_path)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=_app(_service(database), VIEWER)),
            base_url="http://testserver",
        ) as client:
            response = await browser_request(
                client,
                "POST",
                "/api/v1/campaign-briefs",
                json=_body(),
                headers={"Idempotency-Key": "exec05-http-role"},
            )
            assert response.status_code == 403
        async with AsyncClient(
            transport=ASGITransport(app=_app(_service(database))), base_url="http://testserver"
        ) as client:
            response = await client.post(
                "/api/v1/campaign-briefs",
                json=_body(),
                headers={"Idempotency-Key": "exec05-http-csrf"},
            )
            assert response.status_code == 403
        assert await _counts(database) == (0, 0)
    finally:
        await database.dispose()


def test_exec05_brief_openapi_errors_match_shared_problem_media_and_required_retry_key():
    schema = _app(None).openapi()
    for path in ("/api/v1/campaign-briefs", "/api/v1/campaign-briefs/{brief_id}"):
        for operation in schema["paths"][path].values():
            for status, response in operation["responses"].items():
                if status == "default" or int(status) >= 400:
                    assert response["content"] == {
                        "application/problem+json": {
                            "schema": {"$ref": "#/components/schemas/ProblemDetails"}
                        }
                    }
    creation = schema["paths"]["/api/v1/campaign-briefs"]["post"]
    assert {"400", "413", "415", "422"} <= creation["responses"].keys()
    parameter = next(p for p in creation["parameters"] if p["name"] == "Idempotency-Key")
    assert parameter["required"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers,status",
    [
        # Existing outer browser-CSRF policy rejects these before route bounds.
        ([("Content-Type", "text/plain")], 403),
        ([("Content-Type", "application/json"), ("Content-Encoding", "gzip")], 403),
        (
            [
                ("Content-Type", "application/json"),
                ("Idempotency-Key", "first-key"),
                ("Idempotency-Key", "second-key"),
            ],
            400,
        ),
    ],
)
async def test_exec05_brief_rejects_unsupported_transport_and_duplicate_retry_keys(
    tmp_path, headers, status
):
    database = await _database(tmp_path)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=_app(_service(database))), base_url="http://testserver"
        ) as client:
            response = await browser_request(
                client,
                "POST",
                "/api/v1/campaign-briefs",
                content=json.dumps(_body()),
                headers=headers,
            )
            assert response.status_code == status
            assert response.headers["content-type"] == "application/problem+json"
            assert response.headers["cache-control"] == "no-store"
            for canary in (OBJECTIVE, EMAIL, REF):
                assert canary not in response.text
            assert await _counts(database) == (0, 0)
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_exec05_brief_migration_preserves_existing_rows_indexes_and_constraints(tmp_path):
    from tests.integration.db.test_obj_03_artifact_migration import _populated_previous, _state

    database = create_database_runtime(f"sqlite+aiosqlite:///{tmp_path / 'upgrade.db'}")
    try:
        await _populated_previous(database)
        assert await upgrade_database(database, "0009") == "0009"
        before = await _state(database)
        assert before["rows"]["audit_events"]
        async with database.engine.connect() as connection:
            old_checks = await connection.run_sync(
                lambda sync: {
                    c["name"]: c["sqltext"]
                    for c in inspect(sync).get_check_constraints("audit_events")
                }
            )
        assert await upgrade_database(database) == "0010"
        after = await _state(database)
        assert {k: v for k, v in before["rows"].items() if k != "alembic_version"} == {
            k: v for k, v in after["rows"].items() if k != "alembic_version"
        }
        assert before["indexes"] == after["indexes"]
        assert before["foreign_keys"] == after["foreign_keys"]
        async with database.engine.connect() as connection:
            assert await connection.run_sync(schema_matches_metadata)
            new_checks = await connection.run_sync(
                lambda sync: {
                    c["name"]: c["sqltext"]
                    for c in inspect(sync).get_check_constraints("audit_events")
                }
            )
        assert old_checks.keys() == new_checks.keys()
        assert {key for key in old_checks if old_checks[key] != new_checks[key]} == {
            "ck_audit_events_aggregate_links",
            "ck_audit_events_timeline_scope",
        }
        assert new_checks["ck_audit_events_aggregate_links"].endswith(
            old_checks["ck_audit_events_aggregate_links"]
        )
        assert await upgrade_database(database) == "0010"
        assert await _state(database) == after
        created = await _service(database).create(principal=OPERATOR, command=_command())
        assert created.disposition == "created"
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_exec05_brief_database_rejects_audit_shape_drift(tmp_path):
    database = await _database(tmp_path)
    try:
        created = await _service(database).create(principal=OPERATOR, command=_command())
        for values in (
            {"event_type": "campaign_brief.changed"},
            {"mutation_version": 2},
            {"actor_source": "system", "auth_method": "internal"},
            {"aggregate_id": "brief.wrong"},
            {"previous_state": "created", "new_state": "changed"},
        ):
            async with database.engine.connect() as connection:
                transaction = await connection.begin()
                try:
                    with pytest.raises(IntegrityError):
                        await connection.execute(
                            update(AuditEventRecord)
                            .where(AuditEventRecord.aggregate_id == created.resource.id)
                            .values(**values)
                        )
                finally:
                    await transaction.rollback()
        assert await _counts(database) == (1, 1)
    finally:
        await database.dispose()

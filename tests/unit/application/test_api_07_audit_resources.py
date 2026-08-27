"""API-07 global-audit high-watermark, privacy, and cursor behavior."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from types import TracebackType

import pytest
from marketing_agents.application.ports.repositories import AuditFeedPage
from marketing_agents.application.services.audit_events import AuditEventFactory
from marketing_agents.application.services.audit_resources import (
    AUDIT_FEED_ENDPOINT_VERSION,
    AuditListQuery,
    AuditResourceService,
    AuditResourceServiceError,
)
from marketing_agents.domain.audit import AuditContext, AuditEvent
from marketing_agents.domain.entities import Run
from marketing_agents.domain.enums import RunState
from marketing_agents.domain.identity import AuthenticatedPrincipal
from marketing_agents.domain.run_lifecycle import initial_received_transition

from tests.support.identity import human_principal, service_principal

NOW = datetime(2026, 8, 27, 12, tzinfo=UTC)


def _events() -> tuple[AuditEvent, AuditEvent]:
    run = Run(
        id="run.api-07.audit",
        work_item_id="work.api-07.audit",
        state=RunState.RECEIVED,
        catalog_hash="catalog-sha256-v1:" + ("a" * 64),
        configuration_revision=1,
        created_at=NOW,
        updated_at=NOW,
    )
    draft = AuditEventFactory(
        AuditContext.system(
            "service.api-07.audit",
            correlation_id="correlation.api-07.audit",
        )
    ).run_transition(run, initial_received_transition(run))
    return (
        AuditEvent(draft, global_sequence=9002, run_sequence=1, feed_sequence=2),
        AuditEvent(draft, global_sequence=9001, run_sequence=1, feed_sequence=1),
    )


class _Audits:
    def __init__(self, events: tuple[AuditEvent, ...]) -> None:
        self.events = events
        self.calls: list[dict[str, object]] = []

    async def list_feed(self, **values: object) -> AuditFeedPage:
        self.calls.append(values)
        before = values["before_feed_sequence"]
        limit = values["limit"]
        assert type(limit) is int
        assert before is None or type(before) is int
        selected = tuple(
            event for event in self.events if before is None or event.feed_sequence < before
        )
        return AuditFeedPage(high_watermark=2, events=selected[:limit])


class _UnitOfWork:
    def __init__(self, audits: _Audits) -> None:
        self.audits = audits

    async def __aenter__(self) -> _UnitOfWork:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback

    async def commit(self) -> None:
        raise AssertionError("audit reads must never commit")


class _Factory:
    def __init__(self, audits: _Audits) -> None:
        self.unit = _UnitOfWork(audits)
        self.calls = 0

    def __call__(self) -> _UnitOfWork:
        self.calls += 1
        return self.unit


class _ExplodingFactory:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> _UnitOfWork:
        self.calls += 1
        raise AssertionError("authorization must precede unit-of-work access")


def _reader() -> AuthenticatedPrincipal:
    return human_principal(roles=frozenset({"viewer"}), scopes=frozenset())


@pytest.mark.asyncio
async def test_api_07_audit_cursor_binds_endpoint_filters_and_high_watermark() -> None:
    audits = _Audits(_events())
    units = _Factory(audits)
    service = AuditResourceService(
        units,  # type: ignore[arg-type]
        utc_now=lambda: NOW,
    )

    first = await service.list(AuditListQuery(limit=1), principal=_reader())
    second = await service.list(
        AuditListQuery(cursor=first.next_cursor, limit=1),
        principal=_reader(),
    )

    assert first.endpoint_version == AUDIT_FEED_ENDPOINT_VERSION
    assert first.high_watermark == second.high_watermark == 2
    assert first.items[0].feed_sequence == 2
    assert second.items[0].feed_sequence == 1
    assert audits.calls[1]["high_watermark"] == 2
    assert audits.calls[1]["before_feed_sequence"] == 2
    calls_before_rejection = units.calls
    with pytest.raises(AuditResourceServiceError) as captured:
        await service.list(
            AuditListQuery(run_id="run.other", cursor=first.next_cursor, limit=1),
            principal=_reader(),
        )
    assert captured.value.code == "audit_cursor_invalid"
    assert units.calls == calls_before_rejection

    assert first.next_cursor is not None
    prefix, encoded = first.next_cursor.split(".", maxsplit=1)
    raw = base64.urlsafe_b64decode(encoded + ("=" * (-len(encoded) % 4)))
    decoded = json.loads(raw)
    decoded["filter"] = "é" * 64
    non_ascii_raw = json.dumps(
        decoded,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    non_ascii_cursor = (
        prefix + "." + base64.urlsafe_b64encode(non_ascii_raw).decode("ascii").rstrip("=")
    )
    with pytest.raises(AuditResourceServiceError) as non_ascii:
        await service.list(
            AuditListQuery(cursor=non_ascii_cursor, limit=1),
            principal=_reader(),
        )
    assert non_ascii.value.code == "audit_cursor_invalid"
    assert units.calls == calls_before_rejection


@pytest.mark.asyncio
async def test_api_07_audit_projection_hides_internal_identity_and_expires_metadata() -> None:
    event = _events()[0]
    service = AuditResourceService(
        _Factory(_Audits((event,))),  # type: ignore[arg-type]
        utc_now=lambda: event.safe_metadata.expires_at,
    )

    resource = (await service.list(AuditListQuery(), principal=_reader())).items[0]

    assert resource.actor_id.startswith("audit-actor-v1:")
    assert resource.correlation_id.startswith("audit-correlation-v1:")
    assert resource.metadata_expired is True
    assert resource.metadata == {}
    assert not hasattr(resource, "global_sequence")
    assert not hasattr(resource, "audit_url")


@pytest.mark.asyncio
async def test_api_07_audit_authorization_precedes_repository_and_clock() -> None:
    units = _ExplodingFactory()
    service = AuditResourceService(
        units,  # type: ignore[arg-type]
        utc_now=lambda: (_ for _ in ()).throw(AssertionError("clock must not run")),
    )

    with pytest.raises(AuditResourceServiceError) as captured:
        await service.list(AuditListQuery(), principal=service_principal())

    assert captured.value.code == "runtime_human_required"
    assert units.calls == 0

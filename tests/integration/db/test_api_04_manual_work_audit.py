"""API-04: manual intake audit witnesses are atomic, ordered, and redacted."""

from __future__ import annotations

from pathlib import Path

import pytest
from marketing_agents.application.services.manual_work_intake import ManualDryRunServiceError
from marketing_agents.domain.enums import WorkMode
from marketing_agents.infrastructure.db import AuditEventRecord
from sqlalchemy import select

from tests.integration.db.test_api_04_manual_work_persistence import (
    _catalog,
    _command,
    _counts,
    _IncrementingIds,
    _operator,
    _payload,
    _runtime,
    _seed,
    _service,
)


@pytest.mark.asyncio
async def test_api_04_create_replay_and_collision_append_exact_redacted_timeline(
    tmp_path: Path,
) -> None:
    catalog = _catalog()
    runtime = await _runtime(tmp_path / "manual-audit.db")
    await _seed(runtime, catalog)
    raw_key = "manual-api-04-audit-secret-key"
    payload_canary = "manual-api-04-audit-payload-canary"
    try:
        created = await _service(
            runtime,
            catalog,
            mock_connectors_active=True,
            ids=_IncrementingIds(0),
        ).submit(
            _command(key=raw_key, payload=_payload(payload_canary)),
            _operator(),
        )
        replayed = await _service(
            runtime,
            catalog,
            mock_connectors_active=True,
            ids=_IncrementingIds(100),
        ).submit(
            _command(
                key=raw_key,
                payload=_payload(payload_canary),
                correlation_id="correlation.api-04.audit.replay",
            ),
            _operator(),
        )
        with pytest.raises(ManualDryRunServiceError) as collision:
            await _service(
                runtime,
                catalog,
                mock_connectors_active=True,
                ids=_IncrementingIds(200),
            ).submit(
                _command(
                    key=raw_key,
                    payload=_payload("manual-api-04-audit-drift-canary"),
                    mode=WorkMode.MOCK_EXECUTION,
                    correlation_id="correlation.api-04.audit.collision",
                ),
                _operator(),
            )

        assert replayed.work_item.id == created.work_item.id
        assert collision.value.code == "idempotency_conflict"
        assert await _counts(runtime) == (1, 1, 1, 7)
        async with runtime.session_factory() as session:
            records = (
                (
                    await session.execute(
                        select(AuditEventRecord)
                        .where(AuditEventRecord.run_id == created.run.id)
                        .order_by(AuditEventRecord.run_sequence)
                    )
                )
                .scalars()
                .all()
            )

        assert [record.run_sequence for record in records] == list(range(1, 8))
        assert [record.event_type for record in records] == [
            "run.received",
            "ingress.manual_received",
            "work.created",
            "ingress.manual_received",
            "work.duplicate_returned",
            "ingress.manual_received",
            "work.idempotency_collision",
        ]
        assert [record.safe_metadata["receipt_disposition"] for record in records[1:]] == [
            "created",
            "created",
            "replayed",
            "replayed",
            "collision",
            "collision",
        ]
        assert records[-1].outcome == "rejected"
        assert records[-1].reason_code == "idempotency_conflict"
        assert records[-1].mutation_version is None
        assert records[-2].safe_metadata["mode"] == "mock_execution"
        assert records[-1].safe_metadata["mode"] == "mock_execution"
        assert len({record.actor_id for record in records}) == 1
        serialized = repr(
            [(record.event_type, record.aggregate_id, record.safe_metadata) for record in records]
        )
        assert raw_key not in serialized
        assert payload_canary not in serialized
        assert "manual-api-04-audit-drift-canary" not in serialized
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_api_04_schema_rejection_is_a_runless_atomic_redacted_witness(
    tmp_path: Path,
) -> None:
    catalog = _catalog()
    runtime = await _runtime(tmp_path / "manual-schema-rejection-audit.db")
    await _seed(runtime, catalog)
    payload_canary = "manual-api-04-rejected-payload-canary"
    try:
        with pytest.raises(ManualDryRunServiceError) as rejected:
            await _service(runtime, catalog, mock_connectors_active=True).submit(
                _command(payload={"request_id": payload_canary, "source_content": 42}),
                _operator(),
            )

        assert rejected.value.code == "input_schema_invalid"
        assert await _counts(runtime) == (0, 0, 0, 1)
        async with runtime.session_factory() as session:
            record = (await session.execute(select(AuditEventRecord))).scalar_one()
        assert record.event_type == "ingress.schema_rejected"
        assert record.aggregate_type == "manual_ingress_rejection"
        assert record.run_id is None
        assert record.run_sequence is None
        assert record.outcome == "rejected"
        assert record.reason_code == "schema_rejected"
        assert record.mutation_version is None
        assert record.safe_metadata["rejection_code"] == "schema_rejected"
        assert payload_canary not in repr(record.safe_metadata)
        assert "source_content" not in repr(record.safe_metadata)
    finally:
        await runtime.dispose()

"""OBJ-03 scheduler conflicts must not stop unrelated due work or hide integrity defects."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from marketing_agents.application.services.schedule_claiming import ScheduleClaimService
from marketing_agents.application.services.schedule_configuration import (
    ScheduleConfigurationError,
    configured_schedule_id,
)
from marketing_agents.application.services.schedule_processing import (
    ScheduleClaimProcessingError,
    ScheduleClaimProcessingService,
)
from marketing_agents.infrastructure.db.models import WorkItemRecord
from marketing_agents.workers.runtime.composition import build_runtime
from marketing_agents.workers.runtime.scheduler import SchedulerWorker
from sqlalchemy import select

from tests.integration.runtime.test_del_05_process_composition import Clock, _installation
from tests.support.api import browser_request

_INSTANCES = (
    "inst.community.education.course-progress-reminders.01",
    "inst.community.events.live-session-reminder.01",
)


async def _configure(client, instance_id: str) -> None:
    path = f"/api/v1/agent-instances/{instance_id}/configuration"
    current = await client.get(path)
    assert current.status_code == 200, current.text
    schedule = {
        "cron": "* * * * *",
        "timezone": "UTC",
        "misfirePolicy": "run_once",
        "misfireGraceSeconds": 60,
    }
    triggers = [
        item
        for item in current.json()["configuration"]["triggerBindings"]
        if item["type"] != "schedule"
    ]
    triggers.append({"type": "schedule", "enabled": True, **schedule})
    changed = await browser_request(
        client,
        "PATCH",
        path,
        headers={"If-Match": current.headers["etag"]},
        json={
            "triggerBindings": triggers,
            "schedule": schedule,
            "scheduledInput": {
                "input": {
                    "request_id": "request.obj03.scheduler",
                    "source_content": "Original explicit scheduled input.",
                }
            },
        },
    )
    assert changed.status_code == 200, changed.text


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ("after_claim", "before_processing"))
@pytest.mark.parametrize("edit", ("disable", "replace_input"))
async def test_obj_03_configuration_edit_race_leaves_scheduler_usable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    edit: str,
) -> None:
    settings = await _installation(tmp_path)
    clock = Clock()
    runtime = await build_runtime(settings, clock=clock)
    changed_instance = None
    original_claim = ScheduleClaimService.claim_due_once
    original_process = ScheduleClaimProcessingService.process_claimed_once
    try:
        async with AsyncClient(
            transport=ASGITransport(app=runtime.create_app()),
            base_url="http://testserver",
        ) as client:
            for instance_id in _INSTANCES:
                await _configure(client, instance_id)
            clock.current += timedelta(minutes=1)

            async def edit_claimed(claim):
                nonlocal changed_instance
                if changed_instance is not None:
                    return
                changed_instance = next(
                    item for item in _INSTANCES if configured_schedule_id(item) == claim.schedule_id
                )
                path = f"/api/v1/agent-instances/{changed_instance}/configuration"
                current = await client.get(path)
                body = (
                    {"enabled": False}
                    if edit == "disable"
                    else {
                        "scheduledInput": {
                            "input": {
                                "request_id": "request.obj03.scheduler-updated",
                                "source_content": "New explicit input after the original claim.",
                            }
                        },
                    }
                )
                changed = await browser_request(
                    client,
                    "PATCH",
                    path,
                    headers={"If-Match": current.headers["etag"]},
                    json=body,
                )
                assert changed.status_code == 200, changed.text

            async def claim_then_edit(service, *, lease_owner):
                claim = await original_claim(service, lease_owner=lease_owner)
                if claim is not None:
                    await edit_claimed(claim)
                return claim

            async def edit_then_process(service, command):
                await edit_claimed(command.claim)
                return await original_process(service, command)

            if phase == "after_claim":
                monkeypatch.setattr(ScheduleClaimService, "claim_due_once", claim_then_edit)
            else:
                monkeypatch.setattr(
                    ScheduleClaimProcessingService,
                    "process_claimed_once",
                    edit_then_process,
                )
            worker = SchedulerWorker(runtime, "scheduler.obj03.live")
            assert (
                await worker.drain_once()
            )  # Stale claim handled without escaping the process loop.
            assert changed_instance is not None
            async with runtime.database.session_factory() as session:
                assert not list(await session.scalars(select(WorkItemRecord)))
            # Reuse the exact same worker; the unrelated schedule must progress.
            for _ in _INSTANCES:
                await worker.drain_once()
            async with runtime.database.session_factory() as session:
                works = list(await session.scalars(select(WorkItemRecord)))
            other_instance = next(item for item in _INSTANCES if item != changed_instance)
            assert other_instance in {work.agent_instance_id for work in works}
            affected = [work for work in works if work.agent_instance_id == changed_instance]
            if edit == "disable":
                assert not affected
            else:
                assert len(affected) == 1
                assert affected[0].admitted_payload["source_content"] == (
                    "New explicit input after the original claim."
                )
            assert not await worker.drain_once()
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    (
        ScheduleClaimProcessingError("configuration_fence_lost", "unexplained live-claim mismatch"),
        ScheduleClaimProcessingError("claim_fence_lost", "unexplained live-claim mismatch"),
        ScheduleClaimProcessingError("schema_invalid", "schema defect"),
        ScheduleConfigurationError("scheduled input authority does not match"),
        ValueError("runtime_schedule_workflow_binding_invalid"),
    ),
)
async def test_obj_03_scheduler_does_not_swallow_integrity_or_unverified_fence_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    settings = await _installation(tmp_path)
    clock = Clock()
    runtime = await build_runtime(settings, clock=clock)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=runtime.create_app()),
            base_url="http://testserver",
        ) as client:
            await _configure(client, _INSTANCES[0])
        clock.current += timedelta(minutes=1)

        async def broken_processing(_service, _command):
            raise error

        monkeypatch.setattr(
            ScheduleClaimProcessingService,
            "process_claimed_once",
            broken_processing,
        )
        with pytest.raises(type(error)) as observed:
            await SchedulerWorker(runtime, "scheduler.obj03.integrity").drain_once()
        assert observed.value is error
        async with runtime.database.session_factory() as session:
            assert not list(await session.scalars(select(WorkItemRecord)))
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_obj_03_expired_claim_is_a_noop_and_the_same_scheduler_can_reclaim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = await _installation(tmp_path)
    clock = Clock()
    runtime = await build_runtime(settings, clock=clock)
    original = ScheduleClaimProcessingService.process_claimed_once
    expired = False
    try:
        async with AsyncClient(
            transport=ASGITransport(app=runtime.create_app()),
            base_url="http://testserver",
        ) as client:
            await _configure(client, _INSTANCES[0])
        clock.current += timedelta(minutes=1)

        async def expire_once(service, command):
            nonlocal expired
            if not expired:
                expired = True
                clock.current = command.claim.lease_expires_at_utc + timedelta(seconds=1)
            return await original(service, command)

        monkeypatch.setattr(ScheduleClaimProcessingService, "process_claimed_once", expire_once)
        worker = SchedulerWorker(runtime, "scheduler.obj03.expiry")
        assert await worker.drain_once()
        async with runtime.database.session_factory() as session:
            assert not list(await session.scalars(select(WorkItemRecord)))
        assert await worker.drain_once()
        async with runtime.database.session_factory() as session:
            assert len(list(await session.scalars(select(WorkItemRecord)))) == 1
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_obj_03_obsolete_claim_does_not_hide_current_scheduled_input_hmac_corruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = await _installation(tmp_path)
    clock = Clock()
    runtime = await build_runtime(settings, clock=clock)
    original = ScheduleClaimService.claim_due_once
    try:
        async with AsyncClient(
            transport=ASGITransport(app=runtime.create_app()),
            base_url="http://testserver",
        ) as client:
            await _configure(client, _INSTANCES[0])
            clock.current += timedelta(minutes=1)

            async def replace_then_corrupt(service, *, lease_owner):
                claim = await original(service, lease_owner=lease_owner)
                assert claim is not None
                path = f"/api/v1/agent-instances/{_INSTANCES[0]}/configuration"
                current = await client.get(path)
                edited = await browser_request(
                    client,
                    "PATCH",
                    path,
                    headers={"If-Match": current.headers["etag"]},
                    json={"variantLabel": "Updated after claim"},
                )
                assert edited.status_code == 200, edited.text
                # Corrupt only the inner keyed authority while preserving the
                # repository's unkeyed storage checksum and trigger projection.
                async with runtime.dependencies.unit_of_work() as uow:
                    configuration = await uow.configurations.get(_INSTANCES[0])
                    snapshot = configuration.scheduled_input
                    replacement = replace(
                        configuration,
                        scheduled_input=replace(
                            snapshot,
                            authority=replace(
                                snapshot.authority,
                                binding_digest="f" * 64,
                            ),
                        ),
                    ).with_revision(configuration.configuration_revision + 1)
                    assert await uow.configurations.compare_and_swap(configuration, replacement)
                    await uow.commit()
                return claim

            monkeypatch.setattr(ScheduleClaimService, "claim_due_once", replace_then_corrupt)
            with pytest.raises(ScheduleConfigurationError, match="authority does not match"):
                await SchedulerWorker(runtime, "scheduler.obj03.keyed-integrity").drain_once()
            async with runtime.database.session_factory() as session:
                assert not list(await session.scalars(select(WorkItemRecord)))
    finally:
        await runtime.close()

"""AC-07: five real API/worker journeys and exact durable demo provenance."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from marketing_agents.application.policies.json_schema import compile_json_schema
from marketing_agents.application.policies.write_authorization import AuthorizedExternalWrite
from marketing_agents.application.ports.connectors import ConnectorWriteResult
from marketing_agents.application.ports.llm import LLMRequest, LLMResponse
from marketing_agents.application.ports.read_adapter import ReadAdapterRequest, ReadAdapterResult
from marketing_agents.config import Settings
from marketing_agents.demos import DEMO_SCENARIOS
from marketing_agents.domain.action_hash import canonical_action_hash
from marketing_agents.domain.approval import StoredActionApprovalRequest
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.enums import ApprovalStatus, ExternalActionState, RunState, StepState
from marketing_agents.domain.provenance import ArtifactEnvelope, artifact_payload_hash
from marketing_agents.domain.schema_hash import canonical_schema_hash
from marketing_agents.infrastructure.adapters.connectors.dispatch import (
    RegistryConnectorReadAdapter,
    RegistryConnectorWriteGateway,
)
from marketing_agents.infrastructure.adapters.llm.deterministic import DeterministicLLMProvider
from marketing_agents.infrastructure.catalog import compile_catalog
from marketing_agents.infrastructure.catalog.seed import seed_catalog
from marketing_agents.infrastructure.db import create_database_runtime
from marketing_agents.infrastructure.db.local_installation import migrate_local_database
from marketing_agents.infrastructure.db.models import (
    AuditEventRecord,
    CatalogReleaseRecord,
    ConnectorActionReceiptRecord,
    ExecutionAttemptRecord,
    ExternalActionDispatchAttemptRecord,
    ExternalActionRecord,
)
from marketing_agents.infrastructure.scheduling import CroniterRecurrenceCalculator
from marketing_agents.security.redaction import redact_json_pointers
from marketing_agents.workers.runtime.composition import LocalRuntime, build_runtime
from marketing_agents.workers.runtime.run_loop import RunWorker
from sqlalchemy import select

from tests.support.api import browser_request

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "catalog" / "v1"
EMAIL = "demo.email.signup-onboarding.v1"
READ_DEMOS = (
    (
        "demo.social-media.content-draft.v1",
        "social_post_draft",
        "inst.social-media.new-content.linkedin-post-drafter.01",
        "tpl.social-media.new-content.linkedin-post-drafter",
    ),
    (
        "demo.blog-seo.content-review.v1",
        "content_review",
        "inst.blog-seo.new-content.blog-post-updater.01",
        "tpl.blog-seo.new-content.blog-post-updater",
    ),
    (
        "demo.community.reminder-draft.v1",
        "scheduled_reminder_draft",
        "inst.community.events.live-session-reminder.01",
        "tpl.community.events.live-session-reminder",
    ),
    (
        "demo.partnerships.application-review.v1",
        "partner_review_recommendation",
        "inst.partnerships.implementation-partners.partner-application-reviewer.01",
        "tpl.partnerships.implementation-partners.partner-application-reviewer",
    ),
)
EMAIL_AGENTS = (
    (
        "inst.email.newsletter.newsletter-subscriber.01",
        "tpl.email.newsletter.newsletter-subscriber",
    ),
    (
        "inst.email.lifecycle-marketing.customer-onboarder.01",
        "tpl.email.lifecycle-marketing.customer-onboarder",
    ),
)


class Clock:
    def __init__(self) -> None:
        self.current = datetime(2026, 9, 8, 12, tzinfo=UTC)

    def now(self) -> datetime:
        return self.current


@dataclass
class ObservedCalls:
    models: list[LLMRequest] = field(default_factory=list)
    reads: list[ReadAdapterRequest] = field(default_factory=list)
    writes: list[AuthorizedExternalWrite] = field(default_factory=list)


def observe_real_calls(monkeypatch: pytest.MonkeyPatch) -> ObservedCalls:
    """Record entry, but always invoke the original exact production methods."""
    calls = ObservedCalls()
    original_model = DeterministicLLMProvider.generate_structured
    original_read = RegistryConnectorReadAdapter.execute
    original_write = RegistryConnectorWriteGateway.execute

    async def model(self: DeterministicLLMProvider, request: LLMRequest) -> LLMResponse:
        calls.models.append(request)
        return await original_model(self, request)

    async def read(
        self: RegistryConnectorReadAdapter, request: ReadAdapterRequest
    ) -> ReadAdapterResult:
        calls.reads.append(request)
        return await original_read(self, request)

    async def write(
        self: RegistryConnectorWriteGateway, authorization: AuthorizedExternalWrite
    ) -> ConnectorWriteResult:
        calls.writes.append(authorization)
        return await original_write(self, authorization)

    monkeypatch.setattr(DeterministicLLMProvider, "generate_structured", model)
    monkeypatch.setattr(RegistryConnectorReadAdapter, "execute", read)
    monkeypatch.setattr(RegistryConnectorWriteGateway, "execute", write)
    return calls


async def installation(tmp_path: Path) -> Settings:
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'runtime.db'}",
        marketing_agents_digest_key_path=tmp_path / "secrets" / "digest.key",
        catalog_root=CATALOG,
    )
    await migrate_local_database(settings.database_url, settings.marketing_agents_digest_key_path)
    database = create_database_runtime(settings.database_url)
    try:
        await seed_catalog(compile_catalog(CATALOG), database, CroniterRecurrenceCalculator())
    finally:
        await database.dispose()
    return settings


async def submit(client: AsyncClient, scenario_id: str) -> str:
    response = await browser_request(
        client,
        "POST",
        f"/api/v1/demo-scenarios/{scenario_id}/runs",
        json={},
        headers={"Idempotency-Key": f"ac07-{scenario_id}"},
    )
    assert response.status_code == 202, response.text
    run_id = response.json()["runId"]
    assert isinstance(run_id, str)
    queued = await client.get(f"/api/v1/runs/{run_id}")
    assert queued.status_code == 200
    assert queued.json()["state"] == "received"
    return run_id


async def current_requests(
    runtime: LocalRuntime, run_id: str
) -> tuple[StoredActionApprovalRequest, ...]:
    async with runtime.dependencies.unit_of_work() as uow:
        selection = await uow.approvals.get_current_authorization_set(run_id)
        assert selection is not None
        return await uow.approvals.list_current_set(
            run_id,
            selection.authorization_set.plan_hash,
            selection.authorization_set.proposal_revision,
        )


async def persisted_artifact(
    runtime: LocalRuntime, run_id: str, scenario_id: str, clock: Clock
) -> ArtifactEnvelope:
    """Join output to persisted work, plan, producing step, attempt and catalog release."""
    assert {item.id for item in DEMO_SCENARIOS.list()} == {
        "demo.social-media.content-draft.v1",
        "demo.blog-seo.content-review.v1",
        "demo.email.signup-onboarding.v1",
        "demo.community.reminder-draft.v1",
        "demo.partnerships.application-review.v1",
    }
    scenario = DEMO_SCENARIOS.get(scenario_id)
    assert scenario.version == 1 and scenario.workflow_id == scenario_id
    async with runtime.dependencies.unit_of_work() as uow:
        run = await uow.runs.get(run_id)
        assert run is not None and run.state is RunState.COMPLETED
        work = await uow.works.get(run.work_item_id)
        plan = await uow.run_steps.get_plan(run_id)
        steps = await uow.run_steps.list_for_run(run_id)
        artifacts = await uow.artifacts.list_for_run(run_id)
        control = await uow.execution_control.get(run_id)
        history = await uow.runs.list_transitions(run_id)
        assert work is not None and plan is not None and control is not None
        assert len(artifacts) == 1
        artifact = artifacts[0]
        provenance = artifact.provenance
        producing_step = next(step for step in steps if step.id == provenance.step_id)
        assert producing_step.terminal_result
        assert all(step.state is StepState.SUCCEEDED for step in steps)
        assert tuple(
            (step.key, step.selected_instance_id, step.capability_id, step.effect.value)
            for step in steps
        ) == tuple(
            (step.key, step.selected_instance_id, step.capability_id, step.effect)
            for step in scenario.steps
        )
        assert {(step.selected_instance_id, step.template_id) for step in steps} == {
            (agent.instance_id, agent.template_id) for agent in scenario.selected_agents
        }
        assert tuple(item.new_state.value for item in history) == scenario.expected_state_path
        assert (control.model_calls, control.tool_calls) == (
            scenario.expected_model_calls,
            scenario.expected_connector_calls,
        )
        assert (work.workflow_id, work.instance_id, work.input_schema_id) == (
            scenario.workflow_id,
            scenario.primary_instance_id,
            scenario.input_schema_id,
        )
        assert work.input_schema_hash == canonical_schema_hash(scenario.input_schema)
        compile_json_schema(
            scenario.input_schema, expected_schema_id=scenario.input_schema_id
        ).validate(work.admitted_payload, pointer_root="/input", max_depth=16)
        compile_json_schema(
            scenario.output_schema, expected_schema_id=scenario.output_schema_id
        ).validate(artifact.payload, pointer_root="/artifact", max_depth=16)
        assert (provenance.work_item_id, provenance.run_id, provenance.step_id) == (
            work.id,
            run.id,
            producing_step.id,
        )
        assert (provenance.workflow_id, provenance.workflow_version) == (
            scenario.workflow_id,
            str(scenario.version),
        )
        assert (provenance.template_id, provenance.instance_id) == (
            producing_step.template_id,
            producing_step.selected_instance_id,
        )
        assert provenance.instance_config_revision == producing_step.configuration_revision
        assert provenance.admitted_input_digest == work.input_digest
        assert provenance.catalog_hash == run.catalog_hash == plan.catalog_content_hash
        assert provenance.catalog_hash == runtime.catalog.content_hash
        assert provenance.output_schema_id == scenario.output_schema_id
        assert provenance.output_schema_version == "v1"
        assert provenance.output_schema_hash == canonical_schema_hash(scenario.output_schema)
        assert artifact.verify_payload()
        assert provenance.payload_hash == artifact_payload_hash(artifact.payload)
        assert provenance.created_at == clock.now()
        assert provenance.classification == work.input_classification
        input_sources = [source for source in provenance.sources if source.kind == "work_input"]
        assert len(input_sources) == 1
        assert (
            input_sources[0].source_id,
            input_sources[0].integrity_digest,
            input_sources[0].classification,
        ) == (work.id, work.input_digest, work.input_classification)
        assert ("llm", "mock", "mock", "v1") in {
            (provider.provider_kind, provider.mode, provider.name, provider.version)
            for provider in provenance.providers
        }
        if scenario_id != EMAIL:
            assert provenance.parent_artifact_ids == ()
            assert len(provenance.providers) == 1

    async with runtime.database.session_factory() as session:
        release = await session.get(CatalogReleaseRecord, provenance.catalog_hash)
        assert release is not None
        assert release.content_version == runtime.catalog.manifest.content_version == "1.0.0"
        attempts = list(
            (
                await session.scalars(
                    select(ExecutionAttemptRecord).where(ExecutionAttemptRecord.run_id == run_id)
                )
            ).all()
        )
        assert len(attempts) == 1
        assert attempts[0].kind == "model" and attempts[0].outcome == "succeeded"
        assert attempts[0].step_id == provenance.step_id
        assert attempts[0].output_artifact_id == provenance.artifact_id
        assert attempts[0].policy_hash == plan.plan_hash
        assert any(
            source.kind == "external_observation"
            and source.source_id == f"observation:{attempts[0].id}"
            for source in provenance.sources
        )
    return artifact


async def assert_artifact_api(runtime: LocalRuntime, artifact: ArtifactEnvelope) -> None:
    """Public list/detail must project the exact persisted envelope without raw digests."""
    provenance = artifact.provenance
    expected_summary = {
        "id": provenance.artifact_id,
        "work_item_id": provenance.work_item_id,
        "run_id": provenance.run_id,
        "step_id": provenance.step_id,
        "workflow_id": provenance.workflow_id,
        "workflow_version": provenance.workflow_version,
        "template_id": provenance.template_id,
        "instance_id": provenance.instance_id,
        "output_schema_id": provenance.output_schema_id,
        "output_schema_version": provenance.output_schema_version,
        "classification": provenance.classification.value,
        "artifact_url": f"/api/v1/artifacts/{provenance.artifact_id}",
        "run_url": f"/api/v1/runs/{provenance.run_id}",
        "step_url": f"/api/v1/runs/{provenance.run_id}/steps/{provenance.step_id}",
        "template_url": f"/api/v1/agent-templates/{provenance.template_id}",
        "instance_url": f"/api/v1/agent-instances/{provenance.instance_id}",
    }
    async with runtime.dependencies.unit_of_work() as uow:
        steps = await uow.run_steps.list_for_run(provenance.run_id)
    producer = next(step for step in steps if step.id == provenance.step_id)
    material = canonical_json_bytes(
        {
            "artifact_id": provenance.artifact_id,
            "output_schema_hash": provenance.output_schema_hash,
            "payload_hash": provenance.payload_hash,
            "run_id": provenance.run_id,
            "step_id": provenance.step_id,
        }
    )
    expected_digest = (
        "artifact-hmac-sha256-v1:"
        + hmac.new(
            runtime.digest_key.bytes_for_digest(),
            b"marketing-agents:artifact-api-pseudonym:hmac-sha256:v1\x00" + material,
            hashlib.sha256,
        ).hexdigest()
    )
    async with AsyncClient(
        transport=ASGITransport(app=runtime.create_app()), base_url="http://testserver"
    ) as client:
        listed = await client.get(f"/api/v1/runs/{provenance.run_id}/artifacts")
        assert listed.status_code == 200, listed.text
        assert listed.headers["cache-control"] == "no-store"
        listing = listed.json()
        assert listing["run_id"] == provenance.run_id and listing["next_cursor"] is None
        assert len(listing["items"]) == 1
        summary = listing["items"][0]
        assert {key: summary[key] for key in expected_summary} == expected_summary
        assert (
            datetime.fromisoformat(summary["created_at"].replace("Z", "+00:00"))
            == provenance.created_at
        )
        assert "payload_digest" not in summary and "redacted_payload" not in summary
        response = await client.get(expected_summary["artifact_url"])
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        detail = response.json()
        assert {key: detail[key] for key in summary} == summary
        assert detail["catalog_hash"] == provenance.catalog_hash
        assert detail["instance_config_revision"] == provenance.instance_config_revision
        assert detail["output_schema_hash"] == provenance.output_schema_hash
        assert detail["parent_artifact_ids"] == list(provenance.parent_artifact_ids)
        assert detail["sources"] == [
            {
                "kind": source.kind,
                "source_id": source.source_id,
                "classification": source.classification.value,
            }
            for source in provenance.sources
        ]
        assert detail["providers"] == [
            {
                "provider_kind": provider.provider_kind,
                "mode": provider.mode,
                "name": provider.name,
                "version": provider.version,
            }
            for provider in provenance.providers
        ]
        assert detail["redacted_payload"] == redact_json_pointers(
            artifact.payload, producer.result_redaction_fields
        )
        assert detail["payload_digest"] == expected_digest
        assert "payload_hash" not in detail and "admitted_input_digest" not in detail


async def assert_email_zero_calls(
    runtime: LocalRuntime, run_id: str, calls: ObservedCalls, expected_state: RunState
) -> None:
    assert calls.models == calls.reads == calls.writes == []
    async with runtime.dependencies.unit_of_work() as uow:
        run = await uow.runs.get(run_id)
        control = await uow.execution_control.get(run_id)
        assert run is not None and run.state is expected_state
        assert control is not None and (control.model_calls, control.tool_calls) == (0, 0)
        assert await uow.artifacts.list_for_run(run_id) == ()
    async with runtime.database.session_factory() as session:
        assert not (await session.scalars(select(ExecutionAttemptRecord))).all()
        assert not (await session.scalars(select(ExternalActionDispatchAttemptRecord))).all()
        assert not (await session.scalars(select(ConnectorActionReceiptRecord))).all()


@pytest.mark.parametrize(("scenario_id", "artifact_type", "instance_id", "template_id"), READ_DEMOS)
@pytest.mark.asyncio
async def test_ac_07_read_only_demo_real_api_worker_and_persisted_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario_id: str,
    artifact_type: str,
    instance_id: str,
    template_id: str,
) -> None:
    calls = observe_real_calls(monkeypatch)
    settings = await installation(tmp_path)
    clock = Clock()
    runtime = await build_runtime(settings, clock=clock)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=runtime.create_app()), base_url="http://testserver"
        ) as client:
            run_id = await submit(client, scenario_id)
        assert calls.models == calls.reads == calls.writes == []
        # The accepted work must survive a fresh composition before any execution.
        await runtime.close()
        runtime = await build_runtime(settings, clock=clock)
        assert await RunWorker(runtime, "worker.ac07.read").drain_once()
        artifact = await persisted_artifact(runtime, run_id, scenario_id, clock)
        assert (artifact.provenance.instance_id, artifact.provenance.template_id) == (
            instance_id,
            template_id,
        )
        assert tuple(
            (agent.instance_id, agent.template_id)
            for agent in DEMO_SCENARIOS.get(scenario_id).selected_agents
        ) == ((instance_id, template_id),)
        await assert_artifact_api(runtime, artifact)
        assert artifact.payload["artifact_type"] == artifact_type
        assert len(calls.models) == 1
        assert calls.models[0].context.run_id == run_id
        assert calls.models[0].context.step_id == artifact.provenance.step_id
        assert calls.reads == calls.writes == []
        async with runtime.database.session_factory() as session:
            assert not (await session.scalars(select(ExternalActionRecord))).all()
            assert not (await session.scalars(select(ConnectorActionReceiptRecord))).all()
        if scenario_id == "demo.social-media.content-draft.v1":
            assert artifact.payload["publication_status"] == "not_published"
        elif scenario_id == "demo.community.reminder-draft.v1":
            assert artifact.payload["delivery_status"] == "not_sent"
            assert artifact.payload["external_schedule_status"] == "not_externally_scheduled"
        elif scenario_id == "demo.blog-seo.content-review.v1":
            assert artifact.payload["review_status"] == "advisory_only"
        elif scenario_id == "demo.partnerships.application-review.v1":
            assert artifact.payload["advisory_only"] is True
        # Successful completion must survive another process and remain idle.
        await runtime.close()
        runtime = await build_runtime(settings, clock=clock)
        assert not await RunWorker(runtime, "worker.ac07.completed").drain_once()
        async with runtime.dependencies.unit_of_work() as uow:
            assert await uow.artifacts.list_for_run(run_id) == (artifact,)
        assert len(calls.models) == 1 and calls.reads == calls.writes == []
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_ac_07_email_exact_all_approval_barrier_and_receipt_lineage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = observe_real_calls(monkeypatch)
    settings = await installation(tmp_path)
    clock = Clock()
    runtime = await build_runtime(settings, clock=clock)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=runtime.create_app()), base_url="http://testserver"
        ) as client:
            run_id = await submit(client, EMAIL)
            assert calls.models == calls.reads == calls.writes == []
            assert await RunWorker(runtime, "worker.ac07.email.prepare").drain_once()
            await assert_email_zero_calls(runtime, run_id, calls, RunState.AWAITING_APPROVAL)
            requests = await current_requests(runtime, run_id)
            assert len(requests) == 2
            assert len({stored.request.id for stored in requests}) == 2
            assert len({stored.request.action_id for stored in requests}) == 2
            assert len({stored.request.action_hash for stored in requests}) == 2
            assert {stored.status for stored in requests} == {ApprovalStatus.PENDING}
            fixture = DEMO_SCENARIOS.get(EMAIL).fixture
            assert (
                tuple(
                    (agent.instance_id, agent.template_id)
                    for agent in DEMO_SCENARIOS.get(EMAIL).selected_agents
                )
                == EMAIL_AGENTS
            )
            async with runtime.dependencies.unit_of_work() as uow:
                plan = await uow.run_steps.get_plan(run_id)
                assert plan is not None
                proposed = await uow.external_actions.list_run_plan(run_id, plan.plan_hash)
                assert len(proposed) == 2
                proposed_by_capability = {
                    action.envelope.capability_id: action for action in proposed
                }
                assert set(proposed_by_capability) == {
                    "cap.newsletter.subscribe",
                    "cap.crm.upsert-contact",
                }
                newsletter = proposed_by_capability["cap.newsletter.subscribe"]
                crm = proposed_by_capability["cap.crm.upsert-contact"]
                assert newsletter.envelope.action_type == "newsletter.subscribe"
                assert newsletter.connector_binding_id == "mock.newsletter.default"
                assert dict(newsletter.envelope.minimized_payload) == {
                    "contact_ref": fixture["contact_id"],
                    "list_ref": fixture["newsletter_list_ref"],
                }
                assert crm.envelope.action_type == "crm.upsert-contact"
                assert crm.connector_binding_id == "mock.crm.default"
                assert dict(crm.envelope.minimized_payload) == {
                    "contact_ref": fixture["contact_id"],
                    "fields": {
                        "name": fixture["name"],
                        "email": fixture["email"],
                        "consent": fixture["consent"],
                        "signup_at": fixture["signup_at"],
                    },
                }
            first, second = requests
            wrong_hash = "0" * 64 if first.request.action_hash != "0" * 64 else "1" * 64
            rejected = await browser_request(
                client,
                "POST",
                f"/api/v1/approvals/{first.request.id}/approve",
                json={
                    "expected_generation": first.request.generation,
                    "expected_payload_hash": wrong_hash,
                },
            )
            assert rejected.status_code == 409, rejected.text
            assert rejected.json()["code"] == "approval_hash_mismatch"
            await assert_email_zero_calls(runtime, run_id, calls, RunState.AWAITING_APPROVAL)
            response = await browser_request(
                client,
                "POST",
                f"/api/v1/approvals/{first.request.id}/approve",
                json={
                    "expected_generation": first.request.generation,
                    "expected_payload_hash": first.request.action_hash,
                },
            )
            assert response.status_code == 200, response.text
            clock.current += timedelta(seconds=2)
            assert await RunWorker(runtime, "worker.ac07.email.partial").drain_once()
            await assert_email_zero_calls(runtime, run_id, calls, RunState.AWAITING_APPROVAL)
            current = await current_requests(runtime, run_id)
            assert {stored.status for stored in current} == {
                ApprovalStatus.APPROVED,
                ApprovalStatus.PENDING,
            }
            assert all(stored.use is None for stored in current)
            response = await browser_request(
                client,
                "POST",
                f"/api/v1/approvals/{second.request.id}/approve",
                json={
                    "expected_generation": second.request.generation,
                    "expected_payload_hash": second.request.action_hash,
                },
            )
            assert response.status_code == 200, response.text
            # Approval HTTP handling must not dispatch inline.
            await assert_email_zero_calls(runtime, run_id, calls, RunState.EXECUTING)

        await runtime.close()
        clock.current += timedelta(seconds=2)
        runtime = await build_runtime(settings, clock=clock)
        assert await RunWorker(runtime, "worker.ac07.email.approved-restart").drain_once()
        artifact = await persisted_artifact(runtime, run_id, EMAIL, clock)
        assert (artifact.provenance.instance_id, artifact.provenance.template_id) == EMAIL_AGENTS[1]
        await assert_artifact_api(runtime, artifact)
        assert artifact.payload["artifact_type"] == "email_onboarding_summary"
        assert artifact.payload["email_send_status"] == "not_sent"
        # This is one persisted summary envelope with a closed, schema-bound nested
        # welcome draft, not a second persisted artifact or an invented parent edge.
        assert artifact.provenance.parent_artifact_ids == ()
        assert artifact.payload["welcome_artifact"]["artifact_type"] == "welcome_message_draft"
        assert artifact.payload["welcome_artifact"]["recipient_contact_id"] == fixture["contact_id"]
        assert artifact.payload["welcome_artifact"]["delivery_status"] == "draft_only"
        assert artifact.payload["welcome_artifact"]["send_status"] == "not_sent"
        assert artifact.payload["welcome_artifact"]["subject"]
        assert artifact.payload["welcome_artifact"]["body_text"]
        assert len(calls.models) == 1 and calls.reads == [] and len(calls.writes) == 2
        assert {call.action.capability_id for call in calls.writes} == {
            "cap.newsletter.subscribe",
            "cap.crm.upsert-contact",
        }
        assert all(call.action.capability_id != "cap.email.send-message" for call in calls.writes)
        completed_requests = await current_requests(runtime, run_id)
        assert len(completed_requests) == 2
        assert {stored.status for stored in completed_requests} == {ApprovalStatus.CONSUMED}
        by_action = {stored.request.action_id: stored for stored in completed_requests}
        provider_lineage = {}
        async with runtime.dependencies.unit_of_work() as uow:
            plan = await uow.run_steps.get_plan(run_id)
            assert plan is not None
            actions = await uow.external_actions.list_run_plan(run_id, plan.plan_hash)
            assert len(actions) == 2
            assert {action.state for action in actions} == {ExternalActionState.SUCCEEDED}
            refs = {item["action_id"]: item for item in artifact.payload["mock_receipt_refs"]}
            assert len(refs) == 2 and set(refs) == {action.id for action in actions}
            for action in actions:
                stored = by_action[action.id]
                request, decision, use = stored.request, stored.decision, stored.use
                assert decision is not None and use is not None and action.reservation is not None
                assert request.action_hash == decision.action_hash == use.action_hash
                assert (
                    request.action_hash
                    == action.action_hash
                    == canonical_action_hash(action.envelope)
                )
                assert decision.request_id == request.id == use.request_id
                assert decision.id == use.decision_id == action.reservation.approval_decision_id
                assert action.reservation.approval_request_id == request.id
                assert action.reservation.reservation_id == use.reservation_id
                assert request.authorization_set_id == action.envelope.authorization_set_id
                assert use.authorization_set_id == request.authorization_set_id
                assert request.binding_id == action.connector_binding_id
                assert request.capability_id == action.envelope.capability_id
                receipt = await uow.connector_receipts.get(
                    action.connector_binding_id, action.idempotency_key
                )
                assert receipt is not None and action.result is not None
                assert receipt.external_action_id == action.id
                assert receipt.action_hash == request.action_hash
                assert receipt.idempotency_key == action.idempotency_key
                assert receipt.capability_id == action.envelope.capability_id
                assert (
                    receipt.receipt_id == action.result.receipt_id == refs[action.id]["receipt_id"]
                )
                assert receipt.status == refs[action.id]["status"] == "mock_succeeded"
                assert receipt.safe_metadata["external_side_effect"] is False
                expected_family = {
                    "cap.newsletter.subscribe": "newsletter",
                    "cap.crm.upsert-contact": "crm",
                }[action.envelope.capability_id]
                # These values MUST come from this persisted receipt, not runtime
                # registry bindings. The repair records them at actual dispatch.
                assert dict(receipt.safe_metadata) == {
                    "mode": "mock",
                    "external_side_effect": False,
                    "capability_id": action.envelope.capability_id,
                    "provider_kind": "connector",
                    "provider_name": expected_family,
                    "provider_version": "v1",
                }
                assert action.result.safe_metadata == receipt.safe_metadata
                provider_lineage[action.id] = dict(receipt.safe_metadata)
                assert refs[action.id]["binding_id"] == action.connector_binding_id
                assert refs[action.id]["capability_id"] == action.envelope.capability_id
                observed = [call for call in calls.writes if call.action.action_id == action.id]
                assert len(observed) == 1
                assert observed[0].action == action.envelope
                assert observed[0].action_hash == receipt.action_hash
                assert observed[0].idempotency_key == receipt.idempotency_key
                assert observed[0].approval_request_id == request.id
                assert observed[0].approval_decision_id == decision.id

        async with runtime.database.session_factory() as session:
            receipts = list((await session.scalars(select(ConnectorActionReceiptRecord))).all())
            dispatches = list(
                (await session.scalars(select(ExternalActionDispatchAttemptRecord))).all()
            )
            assert len(receipts) == len(dispatches) == 2
            assert {attempt.external_action_id for attempt in dispatches} == set(by_action)
            assert all(
                attempt.attempt_number == 1 and attempt.conclusion == "succeeded"
                for attempt in dispatches
            )
            assert {
                (attempt.external_action_id, attempt.connector_receipt_id) for attempt in dispatches
            } == {(receipt.external_action_id, receipt.receipt_id) for receipt in receipts}
            events = list(
                (
                    await session.scalars(
                        select(AuditEventRecord)
                        .where(AuditEventRecord.run_id == run_id)
                        .order_by(AuditEventRecord.run_sequence)
                    )
                ).all()
            )
            for action_id, stored in by_action.items():
                approval_events = [
                    event
                    for event in events
                    if event.approval_request_id == stored.request.id
                    and event.event_type
                    in {"approval.requested", "approval.approved", "approval.consumed"}
                ]
                assert [event.event_type for event in approval_events] == [
                    "approval.requested",
                    "approval.approved",
                    "approval.consumed",
                ]
                action_events = [event for event in events if event.action_id == action_id]
                for required in (
                    "action.proposed",
                    "action.awaiting_approval",
                    "action.approved",
                    "action.dispatch_reserved",
                    "action.call_started",
                    "action.succeeded",
                ):
                    assert sum(event.event_type == required for event in action_events) == 1
                assert (
                    next(
                        event for event in action_events if event.event_type == "action.succeeded"
                    ).receipt_id
                    == refs[action_id]["receipt_id"]
                )
            consumed = [
                event.run_sequence for event in events if event.event_type == "approval.consumed"
            ]
            started = [
                event.run_sequence for event in events if event.event_type == "action.call_started"
            ]
            assert len(consumed) == len(started) == 2
            assert max(consumed) < min(started)
        await runtime.close()
        runtime = await build_runtime(settings, clock=clock)
        assert not await RunWorker(runtime, "worker.ac07.email.completed").drain_once()
        async with runtime.dependencies.unit_of_work() as uow:
            assert await uow.artifacts.list_for_run(run_id) == (artifact,)
            recovered_actions = await uow.external_actions.list_run_plan(run_id, plan.plan_hash)
            assert len(recovered_actions) == 2
            for action in recovered_actions:
                receipt = await uow.connector_receipts.get(
                    action.connector_binding_id, action.idempotency_key
                )
                assert receipt is not None and action.result is not None
                assert receipt.external_action_id == action.id
                assert receipt.receipt_id == refs[action.id]["receipt_id"]
                assert receipt.action_hash == canonical_action_hash(action.envelope)
                assert dict(receipt.safe_metadata) == provider_lineage[action.id]
                assert action.result.safe_metadata == receipt.safe_metadata
        assert len(calls.writes) == 2 and len(calls.models) == 1

        # Direct producer lineage remains the model; upstream connector lineage is
        # available by exact summary -> action -> immutable receipt joins above.
        assert {
            (provider.provider_kind, provider.mode, provider.name, provider.version)
            for provider in artifact.provenance.providers
        } == {
            ("llm", "mock", "mock", "v1"),
        }
    finally:
        await runtime.close()

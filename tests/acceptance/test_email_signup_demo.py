"""DEMO-03: two exact approvals gate every mock write and the welcome draft."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from marketing_agents.application.orchestration import OrchestrationDependencies
from marketing_agents.application.services.approval_boundaries import ApprovalBoundaryService
from marketing_agents.application.services.approval_decisions import (
    ApprovalDecisionCommand,
    ApprovalDecisionService,
    ApprovalDecisionServiceError,
)
from marketing_agents.application.services.manual_work_intake import ManualDryRunService
from marketing_agents.application.services.plan_persistence import PlanPersistenceError
from marketing_agents.demos import DEMO_SCENARIOS, build_demo_deterministic_provider
from marketing_agents.demos.composition import build_demo_read_adapter
from marketing_agents.demos.email_signup_onboarding import (
    EMAIL_SIGNUP_ONBOARDING_SCENARIO,
)
from marketing_agents.demos.email_signup_service import (
    EmailSignupRunCommand,
    EmailSignupRunService,
    EmailSignupRunServiceError,
)
from marketing_agents.domain.audit import AuditContext
from marketing_agents.domain.enums import (
    ApprovalDecisionKind,
    ApprovalStatus,
    ExternalActionState,
    RunState,
)
from marketing_agents.domain.instance_configuration import InstanceConnectorBinding
from marketing_agents.infrastructure.catalog import compile_catalog
from marketing_agents.infrastructure.catalog.instance_configuration_seed import (
    seed_instance_configurations,
)
from marketing_agents.infrastructure.db import (
    ConnectorActionReceiptRecord,
    InstanceConfigurationSQLAlchemyUnitOfWorkFactory,
    SQLAlchemyManualAdmissionUnitOfWorkFactory,
)
from marketing_agents.infrastructure.manual_work import CompiledCatalogManualAdmissionResolver
from marketing_agents.infrastructure.scheduling.cron_recurrence import (
    CroniterRecurrenceCalculator,
)
from marketing_agents.security.redaction import SecretValue
from sqlalchemy import func, select

from tests.acceptance.test_blog_seo_demo import (
    ADMISSION_KEY,
    CATALOG_ROOT,
    _Clock,
    _factories,
    _Ids,
    _record_provider_calls,
    _runtime,
)
from tests.support.identity import human_principal

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "demos" / "email-signup.json"


async def _configured_service(path: Path):  # type: ignore[no-untyped-def]
    catalog = compile_catalog(CATALOG_ROOT)
    runtime = await _runtime(path)
    configuration_uow = InstanceConfigurationSQLAlchemyUnitOfWorkFactory(runtime.session_factory)
    await seed_instance_configurations(
        catalog,
        configuration_uow,
        CroniterRecurrenceCalculator(),
    )
    async with configuration_uow() as unit_of_work:
        newsletter = await unit_of_work.configurations.get(
            "inst.email.newsletter.newsletter-subscriber.01"
        )
        customer = await unit_of_work.configurations.get(
            "inst.email.lifecycle-marketing.customer-onboarder.01"
        )
        assert newsletter is not None and customer is not None
        assert await unit_of_work.configurations.compare_and_swap(
            newsletter,
            replace(
                newsletter,
                connector_bindings={
                    "newsletter": InstanceConnectorBinding(
                        connector_family="newsletter",
                        binding_id="mock.newsletter.default",
                    )
                },
                configuration_revision=newsletter.configuration_revision + 1,
            ),
        )
        assert await unit_of_work.configurations.compare_and_swap(
            customer,
            replace(
                customer,
                connector_bindings={
                    "crm": InstanceConnectorBinding(
                        connector_family="crm",
                        binding_id="mock.crm.default",
                    )
                },
                configuration_revision=customer.configuration_revision + 1,
            ),
        )
        await unit_of_work.commit()
    dependencies = OrchestrationDependencies(
        _Clock(),
        _Ids(),
        SQLAlchemyManualAdmissionUnitOfWorkFactory(runtime.session_factory, _factories()),
    )
    manual = ManualDryRunService(
        dependencies,
        ADMISSION_KEY,
        CompiledCatalogManualAdmissionResolver(
            catalog,
            mock_connectors_active=True,
            demo_scenarios=DEMO_SCENARIOS,
        ),
        current_catalog_hash=catalog.content_hash,
    )
    provider = build_demo_deterministic_provider(catalog)
    provider_calls = _record_provider_calls(provider)
    return (
        runtime,
        dependencies,
        EmailSignupRunService(
            dependencies,
            manual,
            catalog,
            build_demo_read_adapter(catalog, provider),
        ),
        provider_calls,
    )


def _operator():  # type: ignore[no-untyped-def]
    return human_principal(
        actor_id="principal.test.demo-03-operator",
        roles=frozenset({"operator"}),
        scopes=frozenset({"manual-work:create"}),
    )


def _approver(index: int):  # type: ignore[no-untyped-def]
    return human_principal(
        actor_id=f"principal.test.demo-03-approver-{index}",
        roles=frozenset({"approver"}),
        scopes=frozenset({"approvals:decide", "scope.external-write"}),
    )


async def _requests(dependencies, run_id: str):  # type: ignore[no-untyped-def]
    async with dependencies.unit_of_work() as unit_of_work:
        selection = await unit_of_work.approvals.get_current_authorization_set(run_id)
        assert selection is not None
        return await unit_of_work.approvals.list_current_set(
            run_id,
            selection.authorization_set.plan_hash,
            selection.authorization_set.proposal_revision,
        )


@pytest.mark.asyncio
async def test_demo_03_all_approved_barrier_and_worker_resume(tmp_path: Path) -> None:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    runtime, dependencies, service, provider_calls = await _configured_service(
        tmp_path / "email-signup.db"
    )
    command = EmailSignupRunCommand(
        input_payload=fixture,
        correlation_id="correlation.demo-03.primary",
        idempotency_key=SecretValue("demo-03-idempotency-primary"),
    )
    try:
        prepared = await service.prepare(command, _operator())
        assert prepared.run.state is RunState.AWAITING_APPROVAL
        assert prepared.state_path == EMAIL_SIGNUP_ONBOARDING_SCENARIO.expected_state_path[:4]
        assert (prepared.connector_calls, prepared.model_calls) == (0, 0)
        assert prepared.artifact is None
        assert len(prepared.actions) == prepared.approval_count == 2
        assert {action.state for action in prepared.actions} == {
            ExternalActionState.AWAITING_APPROVAL
        }
        actions_by_capability = {
            action.envelope.capability_id: action for action in prepared.actions
        }
        newsletter_action = actions_by_capability["cap.newsletter.subscribe"]
        assert newsletter_action.envelope.action_type == "newsletter.subscribe"
        assert newsletter_action.connector_binding_id == "mock.newsletter.default"
        assert dict(newsletter_action.envelope.minimized_payload) == {
            "contact_ref": fixture["contact_id"],
            "list_ref": fixture["newsletter_list_ref"],
        }
        crm_action = actions_by_capability["cap.crm.upsert-contact"]
        assert crm_action.envelope.action_type == "crm.upsert-contact"
        assert crm_action.connector_binding_id == "mock.crm.default"
        assert dict(crm_action.envelope.minimized_payload) == {
            "contact_ref": fixture["contact_id"],
            "fields": {
                "name": fixture["name"],
                "email": fixture["email"],
                "consent": fixture["consent"],
                "signup_at": fixture["signup_at"],
            },
        }
        assert provider_calls == []

        requests = await _requests(dependencies, prepared.run.id)
        assert len({item.request.action_id for item in requests}) == 2
        assert len({item.request.action_hash for item in requests}) == 2
        projections = [str(dict(item.request.redacted_projection)) for item in requests]
        assert all(fixture["name"] not in projection for projection in projections)
        assert all(fixture["email"] not in projection for projection in projections)

        first = requests[0].request
        await ApprovalDecisionService(dependencies).decide(
            ApprovalDecisionCommand(
                request_id=first.id,
                expected_generation=first.generation,
                expected_action_hash=first.action_hash,
                decision=ApprovalDecisionKind.APPROVE,
                correlation_id="correlation.demo-03.approval-1",
            ),
            principal=_approver(1),
        )
        after_one = await service.resume(
            prepared.run.id,
            correlation_id="correlation.demo-03.resume-after-one",
        )
        assert after_one.run.state is RunState.AWAITING_APPROVAL
        assert (after_one.connector_calls, after_one.model_calls) == (0, 0)
        assert after_one.artifact is None
        assert provider_calls == []

        current = await _requests(dependencies, prepared.run.id)
        second = next(item.request for item in current if item.status is ApprovalStatus.PENDING)
        await ApprovalDecisionService(dependencies).decide(
            ApprovalDecisionCommand(
                request_id=second.id,
                expected_generation=second.generation,
                expected_action_hash=second.action_hash,
                decision=ApprovalDecisionKind.APPROVE,
                correlation_id="correlation.demo-03.approval-2",
            ),
            principal=_approver(2),
        )
        released = await service.prepare(command, _operator())
        assert released.run.state is RunState.EXECUTING
        assert (released.connector_calls, released.model_calls) == (0, 0)
        assert provider_calls == []
        async with runtime.session_factory() as session:
            receipt_count = int(
                (
                    await session.execute(
                        select(func.count(ConnectorActionReceiptRecord.receipt_id))
                    )
                ).scalar_one()
            )
        assert receipt_count == 0

        completed = await service.resume(
            prepared.run.id,
            correlation_id="correlation.demo-03.resume-complete",
        )
        assert completed.run.state is RunState.COMPLETED
        assert completed.state_path == EMAIL_SIGNUP_ONBOARDING_SCENARIO.expected_state_path
        assert (completed.connector_calls, completed.model_calls) == (2, 1)
        assert all(action.state is ExternalActionState.SUCCEEDED for action in completed.actions)
        assert completed.artifact is not None and completed.artifact.verify_payload()
        assert completed.artifact.payload["artifact_type"] == "email_onboarding_summary"
        assert completed.artifact.payload["email_send_status"] == "not_sent"
        assert len(completed.artifact.payload["mock_receipt_refs"]) == 2
        assert len(provider_calls) == 1
        async with runtime.session_factory() as session:
            completed_receipt_count = int(
                (
                    await session.execute(
                        select(func.count(ConnectorActionReceiptRecord.receipt_id))
                    )
                ).scalar_one()
            )
        assert completed_receipt_count == 2

        replayed = await service.resume(
            prepared.run.id,
            correlation_id="correlation.demo-03.resume-replay",
        )
        assert replayed.run.state is RunState.COMPLETED
        assert replayed.artifact == completed.artifact
        assert len(provider_calls) == 1
        async with runtime.session_factory() as session:
            replayed_receipt_count = int(
                (
                    await session.execute(
                        select(func.count(ConnectorActionReceiptRecord.receipt_id))
                    )
                ).scalar_one()
            )
        assert replayed_receipt_count == 2
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_demo_03_unauthorized_or_drifted_approval_releases_nothing(
    tmp_path: Path,
) -> None:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    runtime, dependencies, service, provider_calls = await _configured_service(
        tmp_path / "email-signup-denied-approval.db"
    )
    try:
        prepared = await service.prepare(
            EmailSignupRunCommand(
                input_payload=fixture,
                correlation_id="correlation.demo-03.denied.prepare",
                idempotency_key=SecretValue("demo-03-idempotency-denied-approval"),
            ),
            _operator(),
        )
        request = (await _requests(dependencies, prepared.run.id))[0].request
        wrong_hash = "0" * 64 if request.action_hash != "0" * 64 else "1" * 64

        with pytest.raises(ApprovalDecisionServiceError) as drifted:
            await ApprovalDecisionService(dependencies).decide(
                ApprovalDecisionCommand(
                    request_id=request.id,
                    expected_generation=request.generation,
                    expected_action_hash=wrong_hash,
                    decision=ApprovalDecisionKind.APPROVE,
                    correlation_id="correlation.demo-03.denied.hash",
                ),
                principal=_approver(1),
            )
        assert drifted.value.code == "approval_hash_mismatch"

        with pytest.raises(ApprovalDecisionServiceError) as unauthorized:
            await ApprovalDecisionService(dependencies).decide(
                ApprovalDecisionCommand(
                    request_id=request.id,
                    expected_generation=request.generation,
                    expected_action_hash=request.action_hash,
                    decision=ApprovalDecisionKind.APPROVE,
                    correlation_id="correlation.demo-03.denied.scope",
                ),
                principal=human_principal(
                    actor_id="principal.test.demo-03-under-scoped",
                    roles=frozenset({"approver"}),
                    scopes=frozenset({"approvals:decide"}),
                ),
            )
        assert unauthorized.value.code == "approval_scope_missing"

        unchanged = await service.resume(
            prepared.run.id,
            correlation_id="correlation.demo-03.denied.resume",
        )
        assert unchanged.run.state is RunState.AWAITING_APPROVAL
        assert (unchanged.connector_calls, unchanged.model_calls) == (0, 0)
        assert unchanged.artifact is None
        assert provider_calls == []
        assert all(
            item.status is ApprovalStatus.PENDING
            for item in await _requests(dependencies, prepared.run.id)
        )
        async with runtime.session_factory() as session:
            assert (
                int(
                    (
                        await session.execute(
                            select(func.count(ConnectorActionReceiptRecord.receipt_id))
                        )
                    ).scalar_one()
                )
                == 0
            )
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_demo_03_configuration_drift_fails_before_plan_commit_or_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    runtime, _dependencies, service, provider_calls = await _configured_service(
        tmp_path / "email-signup-configuration-drift.db"
    )
    original_build_plan = service._build_plan

    async def build_then_drift(work, run):  # type: ignore[no-untyped-def]
        plan, graph, routing = await original_build_plan(work, run)
        configuration_uow = InstanceConfigurationSQLAlchemyUnitOfWorkFactory(
            runtime.session_factory
        )
        async with configuration_uow() as unit_of_work:
            current = await unit_of_work.configurations.get(
                "inst.email.newsletter.newsletter-subscriber.01"
            )
            assert current is not None
            assert await unit_of_work.configurations.compare_and_swap(
                current,
                replace(
                    current,
                    configuration_revision=current.configuration_revision + 1,
                ),
            )
            await unit_of_work.commit()
        return plan, graph, routing

    monkeypatch.setattr(service, "_build_plan", build_then_drift)
    try:
        with pytest.raises(PlanPersistenceError) as captured:
            await service.prepare(
                EmailSignupRunCommand(
                    input_payload=fixture,
                    correlation_id="correlation.demo-03.configuration-drift",
                    idempotency_key=SecretValue("demo-03-idempotency-configuration-drift"),
                ),
                _operator(),
            )
        assert captured.value.code == "plan_instance_configuration_drift"
        assert provider_calls == []
        async with runtime.session_factory() as session:
            assert (
                int(
                    (
                        await session.execute(
                            select(func.count(ConnectorActionReceiptRecord.receipt_id))
                        )
                    ).scalar_one()
                )
                == 0
            )
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_demo_03_pre_release_cancellation_performs_zero_calls(tmp_path: Path) -> None:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    runtime, dependencies, service, provider_calls = await _configured_service(
        tmp_path / "email-signup-cancelled.db"
    )
    try:
        prepared = await service.prepare(
            EmailSignupRunCommand(
                input_payload=fixture,
                correlation_id="correlation.demo-03.cancel.prepare",
                idempotency_key=SecretValue("demo-03-idempotency-cancelled"),
            ),
            _operator(),
        )
        await ApprovalBoundaryService(dependencies).cancel(
            prepared.run.id,
            audit_context=AuditContext.worker(
                "worker.deterministic-demo",
                correlation_id="correlation.demo-03.cancel",
            ),
        )
        cancelled = await service.resume(
            prepared.run.id,
            correlation_id="correlation.demo-03.cancel.resume",
        )
        assert cancelled.run.state is RunState.CANCELLED
        assert (cancelled.connector_calls, cancelled.model_calls) == (0, 0)
        assert cancelled.artifact is None
        assert provider_calls == []
        async with runtime.session_factory() as session:
            assert (
                int(
                    (
                        await session.execute(
                            select(func.count(ConnectorActionReceiptRecord.receipt_id))
                        )
                    ).scalar_one()
                )
                == 0
            )
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_demo_03_validator_rejects_schema_valid_routing_tamper(tmp_path: Path) -> None:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    runtime, dependencies, service, _ = await _configured_service(
        tmp_path / "email-signup-routing-tamper.db"
    )
    try:
        prepared = await service.prepare(
            EmailSignupRunCommand(
                input_payload=fixture,
                correlation_id="correlation.demo-03.tamper.prepare",
                idempotency_key=SecretValue("demo-03-idempotency-routing-tamper"),
            ),
            _operator(),
        )
        async with dependencies.unit_of_work() as unit_of_work:
            inspectable = await unit_of_work.run_steps.get_inspectable_plan(prepared.run.id)
            selection = await unit_of_work.approvals.get_current_authorization_set(prepared.run.id)
            assert inspectable is not None and selection is not None
            actions = await unit_of_work.external_actions.list_run_plan(
                prepared.run.id, inspectable.plan.plan_hash
            )
            approvals = await unit_of_work.approvals.list_current_set(
                prepared.run.id,
                inspectable.plan.plan_hash,
                selection.authorization_set.proposal_revision,
            )

        with pytest.raises(EmailSignupRunServiceError) as rejected:
            service._validate_durable_contract(
                prepared.work_item,
                prepared.run,
                replace(inspectable, assignments=()),
                actions,
                selection,
                approvals,
            )
        assert rejected.value.code == "demo_durable_contract_invalid"
    finally:
        await runtime.dispose()

"""DEMO-03/DEMO-06: exact approvals gate Email mock writes and recovery."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import marketing_agents.demos.email_signup_service as email_signup_service_module
import pytest
from marketing_agents.application.orchestration import OrchestrationDependencies
from marketing_agents.application.services.approval_boundaries import (
    ApprovalBoundaryDisposition,
    ApprovalBoundaryService,
)
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
    ExternalActionRecord,
    InstanceConfigurationSQLAlchemyUnitOfWorkFactory,
    SQLAlchemyManualAdmissionUnitOfWorkFactory,
)
from marketing_agents.infrastructure.manual_work import CompiledCatalogManualAdmissionResolver
from marketing_agents.infrastructure.scheduling.cron_recurrence import (
    CroniterRecurrenceCalculator,
)
from marketing_agents.security.redaction import SecretValue
from sqlalchemy import func, select, update

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


class _MutableClock:
    def __init__(self) -> None:
        self.current = _Clock().now()

    def now(self) -> datetime:
        return self.current


async def _configured_service(path: Path, *, clock=None):  # type: ignore[no-untyped-def]
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
        _Clock() if clock is None else clock,
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


async def _receipt_count(runtime) -> int:  # type: ignore[no-untyped-def]
    async with runtime.session_factory() as session:
        return int(
            (
                await session.execute(select(func.count(ConnectorActionReceiptRecord.receipt_id)))
            ).scalar_one()
        )


class _SimulatedProcessCrash(BaseException):
    pass


class _CrashAfterDurableReceiptGateway:
    def __init__(self, delegate: Any, state: dict[str, Any]) -> None:
        self._delegate = delegate
        self._state = state

    def contract_for(self, action):  # type: ignore[no-untyped-def]
        return self._delegate.contract_for(action)

    async def execute(self, authorization):  # type: ignore[no-untyped-def]
        self._state["physical_calls"] += 1
        result = await self._delegate.execute(authorization)
        if self._state["crash_pending"]:
            self._state["crash_pending"] = False
            raise _SimulatedProcessCrash
        return result


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


@pytest.mark.parametrize("case", ("expired", "rejected", "reused"))
@pytest.mark.asyncio
async def test_demo_06_expired_rejected_or_reused_approval_releases_nothing(
    tmp_path: Path,
    case: str,
) -> None:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    clock = _MutableClock()
    runtime, dependencies, service, provider_calls = await _configured_service(
        tmp_path / f"email-signup-demo-06-{case}.db",
        clock=clock,
    )
    try:
        prepared = await service.prepare(
            EmailSignupRunCommand(
                input_payload=fixture,
                correlation_id=f"correlation.demo-06.{case}.prepare",
                idempotency_key=SecretValue(f"demo-06-idempotency-{case}"),
            ),
            _operator(),
        )
        request = (await _requests(dependencies, prepared.run.id))[0].request
        command = ApprovalDecisionCommand(
            request_id=request.id,
            expected_generation=request.generation,
            expected_action_hash=request.action_hash,
            decision=(
                ApprovalDecisionKind.REJECT if case == "rejected" else ApprovalDecisionKind.APPROVE
            ),
            correlation_id=f"correlation.demo-06.{case}.decision",
        )
        if case == "expired":
            await ApprovalDecisionService(dependencies).decide(
                command,
                principal=_approver(1),
            )
            current = await _requests(dependencies, prepared.run.id)
            clock.current = min(stored.request.expires_at for stored in current)
            expired = await ApprovalBoundaryService(dependencies).evaluate(
                prepared.run.id,
                audit_context=AuditContext.worker(
                    "worker.deterministic-demo",
                    correlation_id="correlation.demo-06.expired.evaluate",
                ),
            )
            assert expired.disposition is ApprovalBoundaryDisposition.EXPIRED
            async with dependencies.unit_of_work() as unit_of_work:
                inspectable = await unit_of_work.run_steps.get_inspectable_plan(prepared.run.id)
                assert inspectable is not None
                actions = await unit_of_work.external_actions.list_run_plan(
                    prepared.run.id,
                    inspectable.plan.plan_hash,
                )
                current = await unit_of_work.approvals.list_current_set(
                    prepared.run.id,
                    inspectable.plan.plan_hash,
                    current[0].request.proposal_revision,
                )
            assert all(stored.use is None for stored in current)
            assert all(action.reservation is None for action in actions)
            assert all(
                action.state
                not in {
                    ExternalActionState.DISPATCH_RESERVED,
                    ExternalActionState.DISPATCHING,
                    ExternalActionState.SUCCEEDED,
                }
                for action in actions
            )
        else:
            await ApprovalDecisionService(dependencies).decide(
                command,
                principal=_approver(1),
            )
            if case == "reused":
                with pytest.raises(ApprovalDecisionServiceError) as captured:
                    await ApprovalDecisionService(dependencies).decide(
                        command,
                        principal=_approver(2),
                    )
                assert captured.value.code == "approval_decision_conflict"

        snapshot = await service.resume(
            prepared.run.id,
            correlation_id=f"correlation.demo-06.{case}.resume",
        )
        assert snapshot.run.state is (
            RunState.REJECTED if case == "rejected" else RunState.AWAITING_APPROVAL
        )
        assert (snapshot.connector_calls, snapshot.model_calls) == (0, 0)
        assert snapshot.artifact is None
        assert provider_calls == []
        assert await _receipt_count(runtime) == 0
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_demo_06_changed_approved_action_prevents_barrier_release(
    tmp_path: Path,
) -> None:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    runtime, dependencies, service, provider_calls = await _configured_service(
        tmp_path / "email-signup-demo-06-changed-action.db"
    )
    try:
        prepared = await service.prepare(
            EmailSignupRunCommand(
                input_payload=fixture,
                correlation_id="correlation.demo-06.changed.prepare",
                idempotency_key=SecretValue("demo-06-idempotency-changed-action"),
            ),
            _operator(),
        )
        requests = tuple(
            stored.request for stored in await _requests(dependencies, prepared.run.id)
        )
        approved_request, release_request = requests
        await ApprovalDecisionService(dependencies).decide(
            ApprovalDecisionCommand(
                request_id=approved_request.id,
                expected_generation=approved_request.generation,
                expected_action_hash=approved_request.action_hash,
                decision=ApprovalDecisionKind.APPROVE,
                correlation_id="correlation.demo-06.changed.first-approval",
            ),
            principal=_approver(1),
        )
        async with runtime.session_factory() as session, session.begin():
            record = await session.get(ExternalActionRecord, approved_request.action_id)
            assert record is not None
            changed_envelope = dict(record.canonical_envelope)
            changed_payload = dict(changed_envelope["minimized_payload"])
            changed_payload["contact_ref"] = "contact.synthetic.changed"
            changed_envelope["minimized_payload"] = changed_payload
            await session.execute(
                update(ExternalActionRecord)
                .where(ExternalActionRecord.id == approved_request.action_id)
                .values(canonical_envelope=changed_envelope)
            )

        with pytest.raises(ApprovalDecisionServiceError) as captured:
            await ApprovalDecisionService(dependencies).decide(
                ApprovalDecisionCommand(
                    request_id=release_request.id,
                    expected_generation=release_request.generation,
                    expected_action_hash=release_request.action_hash,
                    decision=ApprovalDecisionKind.APPROVE,
                    correlation_id="correlation.demo-06.changed.release-attempt",
                ),
                principal=_approver(2),
            )
        assert captured.value.code == "approval_record_corrupt"
        async with runtime.session_factory() as session:
            states = tuple(
                (
                    await session.execute(
                        select(ExternalActionRecord.state).where(
                            ExternalActionRecord.run_id == prepared.run.id
                        )
                    )
                ).scalars()
            )
        assert set(states) == {
            ExternalActionState.APPROVED.value,
            ExternalActionState.AWAITING_APPROVAL.value,
        }
        assert provider_calls == []
        assert await _receipt_count(runtime) == 0
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_demo_06_post_receipt_crash_recovers_without_duplicate_side_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    clock = _MutableClock()
    runtime, dependencies, service, provider_calls = await _configured_service(
        tmp_path / "email-signup-demo-06-post-receipt-crash.db",
        clock=clock,
    )
    command = EmailSignupRunCommand(
        input_payload=fixture,
        correlation_id="correlation.demo-06.crash.prepare",
        idempotency_key=SecretValue("demo-06-idempotency-post-receipt-crash"),
    )
    real_gateway = email_signup_service_module.RegistryConnectorWriteGateway
    gateway_state: dict[str, Any] = {"physical_calls": 0, "crash_pending": True}

    def crash_gateway(*args, **kwargs):  # type: ignore[no-untyped-def]
        return _CrashAfterDurableReceiptGateway(real_gateway(*args, **kwargs), gateway_state)

    try:
        prepared = await service.prepare(command, _operator())
        for index, stored in enumerate(await _requests(dependencies, prepared.run.id), start=1):
            request = stored.request
            await ApprovalDecisionService(dependencies).decide(
                ApprovalDecisionCommand(
                    request_id=request.id,
                    expected_generation=request.generation,
                    expected_action_hash=request.action_hash,
                    decision=ApprovalDecisionKind.APPROVE,
                    correlation_id=f"correlation.demo-06.crash.approval-{index}",
                ),
                principal=_approver(index),
            )
        monkeypatch.setattr(
            email_signup_service_module,
            "RegistryConnectorWriteGateway",
            crash_gateway,
        )

        with pytest.raises(_SimulatedProcessCrash):
            await service.resume(
                prepared.run.id,
                correlation_id="correlation.demo-06.crash.first-resume",
            )
        assert gateway_state["physical_calls"] == 1
        assert await _receipt_count(runtime) == 1
        assert provider_calls == []

        async with dependencies.unit_of_work() as unit_of_work:
            inspectable = await unit_of_work.run_steps.get_inspectable_plan(prepared.run.id)
            assert inspectable is not None
            actions = await unit_of_work.external_actions.list_run_plan(
                prepared.run.id,
                inspectable.plan.plan_hash,
            )
        crashed = next(
            action for action in actions if action.state is ExternalActionState.DISPATCHING
        )
        assert crashed.call_deadline_at is not None and crashed.lease is not None
        assert crashed.call_deadline_at < crashed.lease.expires_at

        clock.current = crashed.call_deadline_at
        pending = await service.resume(
            prepared.run.id,
            correlation_id="correlation.demo-06.crash.pending-resume",
        )
        assert pending.run.state is RunState.EXECUTING
        assert (pending.connector_calls, pending.model_calls) == (1, 0)
        assert gateway_state["physical_calls"] == 1
        assert await _receipt_count(runtime) == 1
        assert provider_calls == []

        clock.current = crashed.lease.expires_at
        completed = await service.resume(
            prepared.run.id,
            correlation_id="correlation.demo-06.crash.recovered-resume",
        )
        assert completed.run.state is RunState.COMPLETED
        assert (completed.connector_calls, completed.model_calls) == (2, 1)
        assert gateway_state["physical_calls"] == 2
        assert await _receipt_count(runtime) == 2
        assert len(provider_calls) == 1

        replayed = await service.resume(
            prepared.run.id,
            correlation_id="correlation.demo-06.crash.replay",
        )
        assert replayed.artifact == completed.artifact
        assert gateway_state["physical_calls"] == 2
        assert await _receipt_count(runtime) == 2
        assert len(provider_calls) == 1
    finally:
        await runtime.dispose()

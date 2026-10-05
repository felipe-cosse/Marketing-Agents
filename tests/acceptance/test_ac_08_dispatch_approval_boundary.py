"""AC-08: actively reject dispatch before the exact durable all-approvals release."""

from __future__ import annotations

from dataclasses import asdict
from datetime import timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from marketing_agents.application.policies.write_authorization import WriteAuthorizationGuard
from marketing_agents.application.ports.connectors import (
    AuthorizedConnectorCommand,
    ConnectorPortError,
)
from marketing_agents.application.services.external_action_dispatcher import (
    DispatchDisposition,
    ExternalActionDispatcher,
    ExternalActionDispatchError,
)
from marketing_agents.domain.action_hash import canonical_action_hash
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.enums import ApprovalStatus, ExternalActionState, RunState, StepState
from marketing_agents.infrastructure.adapters.connectors.composition import (
    build_durable_connector_bundle,
)
from marketing_agents.infrastructure.adapters.connectors.dispatch import (
    RegistryConnectorWriteGateway,
)
from marketing_agents.infrastructure.db.models import (
    ApprovalUseRecord,
    ConnectorActionReceiptRecord,
    ExternalActionDispatchAttemptRecord,
)
from marketing_agents.workers.runtime.composition import build_runtime
from marketing_agents.workers.runtime.run_loop import RunWorker
from sqlalchemy import select

from tests.acceptance.test_ac_07_real_composition_demos import (
    EMAIL,
    Clock,
    assert_email_zero_calls,
    current_requests,
    installation,
    observe_real_calls,
    submit,
)
from tests.support.api import browser_request


async def snapshot(runtime, run_id):
    async with runtime.dependencies.unit_of_work() as uow:
        run = await uow.runs.get(run_id)
        plan = await uow.run_steps.get_plan(run_id)
        assert run is not None and plan is not None
        steps = await uow.run_steps.list_for_run(run_id)
        actions = await uow.external_actions.list_run_plan(run_id, plan.plan_hash)
    return run, steps, actions, await current_requests(runtime, run_id)


async def reject_dispatch_without_release(runtime, run_id, dispatcher, calls, suffix):
    """Actually try each valid persisted action ID; unchanged evidence is the outcome."""
    before = await snapshot(runtime, run_id)
    run, steps, actions, requests = before
    assert run.state is RunState.AWAITING_APPROVAL
    assert len(actions) == len(requests) == 2
    assert all(action.reservation is None and action.result is None for action in actions)
    assert all(stored.use is None for stored in requests)
    write_steps = [step for step in steps if step.effect.value == "write"]
    assert len(write_steps) == 2
    assert {step.id for step in write_steps} == {action.step_id for action in actions}
    async with runtime.dependencies.unit_of_work() as uow:
        assert all(step.state is StepState.AWAITING_APPROVAL for step in write_steps)
        for action in actions:
            assert await uow.approvals.get_release_authority(action.id) is None
    for action in actions:
        with pytest.raises(ExternalActionDispatchError) as denied:
            await dispatcher.dispatch_once(action.id, lease_owner=f"worker.ac08.{suffix}")
        assert denied.value.code == "action_not_dispatchable"
    assert await snapshot(runtime, run_id) == before
    await assert_email_zero_calls(runtime, run_id, calls, RunState.AWAITING_APPROVAL)
    async with runtime.database.session_factory() as session:
        assert not (await session.scalars(select(ApprovalUseRecord))).all()


@pytest.mark.asyncio
async def test_ac_08_real_dispatcher_and_mock_reject_unapproved_then_accept_exact_release(
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
            assert await RunWorker(runtime, "worker.ac08.prepare").drain_once()
            run, _, actions, requests = await snapshot(runtime, run_id)
            assert run.state is RunState.AWAITING_APPROVAL
            assert {action.envelope.capability_id for action in actions} == {
                "cap.newsletter.subscribe",
                "cap.crm.upsert-contact",
            }
            assert {action.state for action in actions} == {ExternalActionState.AWAITING_APPROVAL}
            assert {stored.status for stored in requests} == {ApprovalStatus.PENDING}
            bundle = build_durable_connector_bundle(
                runtime.catalog,
                unit_of_work_factory=runtime.dependencies.unit_of_work_factory,
                clock=runtime.dependencies.clock,
            )
            assert bundle.ledger.durable
            gateway = RegistryConnectorWriteGateway(
                bundle.registry,
                bundle,
                binding_configuration_revisions={
                    action.connector_binding_id: (
                        action.delivery_contract.binding_configuration_revision
                    )
                    for action in actions
                },
            )
            dispatcher = ExternalActionDispatcher(
                runtime.dependencies, gateway, WriteAuthorizationGuard()
            )

            # Prove these are viable registered writes with an exact schema/binding,
            # then attempt the real connector operation without a sealed proof.
            # Malformed payload/binding rejection must not be the reason this passes.
            for action in actions:
                assert asdict(gateway.contract_for(action)) == asdict(action.delivery_contract)
                operation = bundle.registry.resolve(action.envelope.capability_id)
                command = operation.request_type.model_validate_json(
                    canonical_json_bytes(action.envelope.minimized_payload), strict=True
                )
                handler = bundle.binding_registry.resolve(action.connector_binding_id).handlers[
                    action.envelope.capability_id
                ]
                for unsealed in (None, action.envelope):
                    with pytest.raises(ConnectorPortError) as denied:
                        await handler(
                            AuthorizedConnectorCommand(
                                authorization=unsealed,  # type: ignore[arg-type]
                                command=command,
                            )
                        )
                    assert denied.value.code == "authorization_mismatch"
            assert bundle.ledger.side_effect_count == 0
            await reject_dispatch_without_release(runtime, run_id, dispatcher, calls, "pending")

            first, second = requests
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
            _, _, partly_approved, current = await snapshot(runtime, run_id)
            assert {action.state for action in partly_approved} == {
                ExternalActionState.APPROVED,
                ExternalActionState.AWAITING_APPROVAL,
            }
            assert {stored.status for stored in current} == {
                ApprovalStatus.APPROVED,
                ApprovalStatus.PENDING,
            }
            # Even the individually approved action remains non-dispatchable.
            await reject_dispatch_without_release(runtime, run_id, dispatcher, calls, "partial")
            assert bundle.ledger.side_effect_count == 0

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
            # The final decision service atomically consumes/reserves the exact set,
            # but the HTTP request itself never calls a model or connector.
            await assert_email_zero_calls(runtime, run_id, calls, RunState.EXECUTING)
            run, steps, reserved, consumed = await snapshot(runtime, run_id)
            assert {item.status for item in consumed} == {ApprovalStatus.CONSUMED}
            assert all(item.use is not None for item in consumed)
            assert {action.state for action in reserved} == {ExternalActionState.DISPATCH_RESERVED}
            assert {action.id for action in reserved} == {action.id for action in actions}
            assert all(action.reservation is not None for action in reserved)
            assert all(
                step.state is StepState.READY for step in steps if step.effect.value == "write"
            )

        # Positive control: same dispatcher, same mock bundle, same exact action IDs.
        # It must really dispatch successfully; a permanently disabled gateway cannot
        # make the preceding negative assertions look like adequate protection.
        by_request_action = {stored.request.action_id: stored for stored in consumed}
        for action in reserved:
            result = await dispatcher.dispatch_once(action.id, lease_owner="worker.ac08.authorized")
            assert result.disposition is DispatchDisposition.SUCCEEDED
            assert result.action.state is ExternalActionState.SUCCEEDED
            assert result.action.result is not None
            stored = by_request_action[action.id]
            assert stored.decision is not None and stored.use is not None
            proofs = [proof for proof in calls.writes if proof.action.action_id == action.id]
            assert len(proofs) == 1
            proof = proofs[0]
            assert proof.action == action.envelope
            assert (
                proof.action_hash
                == stored.request.action_hash
                == canonical_action_hash(action.envelope)
            )
            assert proof.approval_request_id == stored.request.id
            assert proof.approval_decision_id == stored.decision.id
            assert proof.reservation_id == stored.use.reservation_id
            assert proof.idempotency_key == action.idempotency_key
            async with runtime.dependencies.unit_of_work() as uow:
                receipt = await uow.connector_receipts.get(
                    action.connector_binding_id, action.idempotency_key
                )
                assert receipt is not None
                assert receipt.external_action_id == action.id
                assert receipt.action_hash == proof.action_hash
                assert receipt.receipt_id == result.action.result.receipt_id
                assert receipt.status == "mock_succeeded"
        assert len(calls.writes) == bundle.ledger.side_effect_count == 2
        assert calls.models == calls.reads == []
        async with runtime.database.session_factory() as session:
            receipts = (await session.scalars(select(ConnectorActionReceiptRecord))).all()
            attempts = (await session.scalars(select(ExternalActionDispatchAttemptRecord))).all()
            assert len(receipts) == len(attempts) == 2
            assert {attempt.external_action_id for attempt in attempts} == {
                action.id for action in actions
            }
            assert all(
                attempt.attempt_number == 1 and attempt.conclusion == "succeeded"
                for attempt in attempts
            )

        # Completing the same real workflow proves rejected early attempts did not
        # poison legitimate execution. The worker finds succeeded writes and only
        # generates the welcome draft, without a third connector invocation.
        clock.current += timedelta(seconds=2)
        assert await RunWorker(runtime, "worker.ac08.finish").drain_once()
        completed, _, completed_actions, _ = await snapshot(runtime, run_id)
        assert completed.state is RunState.COMPLETED
        assert {action.state for action in completed_actions} == {ExternalActionState.SUCCEEDED}
        assert len(calls.writes) == 2 and len(calls.models) == 1 and calls.reads == []
    finally:
        await runtime.close()

"""OBJ-05: independent offline adapters use unchanged admission and execution services.

These are deployment-composition qualification tests, not a live-provider mode.
The public runtime remains mock-only; the explicit factory substitution supplies
an independent local connector for an already registered local binding.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from marketing_agents.application.policies.write_authorization import AuthorizedExternalWrite
from marketing_agents.application.ports.connector_families import SubscribeContactCommand
from marketing_agents.application.ports.connectors import (
    AuthorizedConnectorCommand,
    ConnectorWriteResult,
)
from marketing_agents.application.ports.llm import LLMRequest, LLMResponse, LLMUsage
from marketing_agents.domain.action_hash import canonical_action_hash
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.entities import ConnectorActionReceipt
from marketing_agents.domain.enums import ExternalActionState, RunState, StepState, TriggerKind
from marketing_agents.infrastructure.adapters.catalog_roles import CatalogRoleRenderer
from marketing_agents.infrastructure.adapters.connectors.bindings import (
    ConnectorBindingRegistration,
    ConnectorBindingRegistry,
)
from marketing_agents.infrastructure.adapters.connectors.registry import (
    OPERATION_REGISTRATIONS,
    ConnectorOperationRegistry,
)
from marketing_agents.infrastructure.adapters.llm.read_adapter import (
    LLMReadBinding,
    StructuredLLMReadAdapter,
)
from marketing_agents.infrastructure.runtime import catalog_write_workflows
from marketing_agents.infrastructure.runtime.catalog_workflows import CatalogReadWorkflowService
from marketing_agents.workers.runtime.composition import build_runtime
from marketing_agents.workers.runtime.run_loop import RunWorker

from tests.integration.runtime.test_del_05_process_composition import Clock, _installation
from tests.support.api import browser_request

_MODEL_INSTANCE = "inst.social-media.new-content.linkedin-post-drafter.01"
_MODEL_TEMPLATE = "tpl.social-media.new-content.linkedin-post-drafter"
_WRITE_INSTANCE = "inst.email.newsletter.newsletter-subscriber.01"
_BINDING_ID = "mock.newsletter.default"
_WRITE_CAPABILITY = "cap.newsletter.subscribe"
_COMMAND = {"contact_ref": "contact.obj05.local", "list_ref": "list.obj05.local"}


class _IndependentStructuredProvider:
    """No mock provider/renderer inheritance or delegation, credentials, or I/O."""

    def __init__(self, *, malformed: bool) -> None:
        self.malformed = malformed
        self.requests: list[LLMRequest] = []

    async def generate_structured(self, request: LLMRequest) -> LLMResponse:
        assert type(request) is LLMRequest
        self.requests.append(request)
        admitted = json.loads(request.retrieved_content[0].content)
        payload = {
            "artifact_id": "artifact_obj05_independent",
            "summary": "Independent local provider output.",
            "artifact": f"Independent draft: {admitted['source_content']}",
            "proposed_actions": [],
            "provenance": {
                "template_id": request.system_instructions.template_id,
                "source_request_id": admitted["request_id"],
            },
        }
        if self.malformed:
            payload.pop("artifact")
        return LLMResponse(
            structured_payload=payload,
            provider="independent-local-model",
            model="qualification-model",
            version="qualification-v1",
            finish_reason="complete",
            usage=LLMUsage(input_tokens=20, output_tokens=25),
        )


def _compose_model(runtime, provider):
    definition = runtime.workflows.for_catalog_role(_MODEL_TEMPLATE, TriggerKind.MANUAL)
    binding = LLMReadBinding(
        scenario_id=definition.id,
        template_id=_MODEL_TEMPLATE,
        instance_id=_MODEL_INSTANCE,
        capability_id="cap.model.generate-structured",
        input_schema_id=definition.input_schema_id,
        input_schema=definition.input_schema,
        model_output_schema_id=definition.output_schema_id,
        model_output_schema=definition.output_schema,
        output_schema_id=definition.output_schema_id,
        output_schema=definition.output_schema,
        catalog_content_hash=runtime.catalog.content_hash.split(":", 1)[1],
        system_prompt=runtime.catalog.prompt_text_by_template[_MODEL_TEMPLATE].strip(),
        provider_mode="local",
        provider_name="independent-local-model",
        provider_version="qualification-v1",
        # Leave validation to the unchanged ControlledReadExecutor; an invalid
        # provider payload must not become an artifact merely through this seam.
        output_transform=dict,
    )
    runtime.catalog_reads = CatalogReadWorkflowService(
        runtime.dependencies,
        runtime.catalog,
        runtime.workflows,
        StructuredLLMReadAdapter(provider, (binding,)),
        CatalogRoleRenderer(runtime.catalog),
    )


class _IndependentNewsletter:
    """A port implementation with its own proof checks and durable local receipt."""

    def __init__(self, registry, unit_of_work_factory, clock) -> None:
        self.registry = registry
        self.unit_of_work_factory = unit_of_work_factory
        self.clock = clock
        self.calls: list[AuthorizedConnectorCommand[SubscribeContactCommand]] = []
        self.inserted_receipts = 0

    async def subscribe(
        self, request: AuthorizedConnectorCommand[SubscribeContactCommand]
    ) -> ConnectorWriteResult:
        assert type(request) is AuthorizedConnectorCommand
        assert type(request.command) is SubscribeContactCommand
        authorization = request.authorization
        assert type(authorization) is AuthorizedExternalWrite
        # Reconstructing the sealed proof invokes its public invariant check;
        # neither this connector nor the test constructs or imports its seal.
        assert replace(authorization) == authorization
        action = authorization.action
        registration = self.registry.resolve(_WRITE_CAPABILITY)
        assert action.capability_id == _WRITE_CAPABILITY
        assert action.connector_family == "newsletter"
        assert action.binding_id == _BINDING_ID
        assert action.payload_schema_id == registration.metadata.request_schema_id
        assert authorization.action_hash == canonical_action_hash(action)
        assert canonical_json_bytes(request.command.model_dump(mode="json")) == (
            canonical_json_bytes(action.minimized_payload)
        )
        assert request.command.model_dump(mode="json") == _COMMAND
        self.calls.append(request)
        candidate = ConnectorActionReceipt(
            external_action_id=action.action_id,
            connector_binding_id=action.binding_id,
            idempotency_key=authorization.idempotency_key,
            action_hash=authorization.action_hash,
            capability_id=action.capability_id,
            receipt_id=f"independent-receipt:{authorization.action_hash[:24]}",
            status="succeeded",
            safe_metadata={"external_side_effect": False, "provider": "independent-newsletter"},
            created_at=self.clock.now(),
        )
        async with self.unit_of_work_factory() as uow:
            stored = await uow.connector_receipts.add_or_get(candidate)
            await uow.commit()
        self.inserted_receipts += int(stored.inserted)
        # The real dispatcher independently checks this committed receipt. A
        # successful-looking ConnectorWriteResult alone would not be sufficient.
        return ConnectorWriteResult(
            receipt_id=stored.receipt.receipt_id,
            status=stored.receipt.status,
            safe_metadata=dict(stored.receipt.safe_metadata),
        )


def _inject_newsletter_factory(monkeypatch):
    instances = []

    def factory(catalog, *, unit_of_work_factory, clock):
        registry = ConnectorOperationRegistry(OPERATION_REGISTRATIONS)
        registry.validate_catalog(catalog)
        connector = _IndependentNewsletter(registry, unit_of_work_factory, clock)
        instances.append(connector)
        return ConnectorBindingRegistry(
            registry,
            (
                ConnectorBindingRegistration(
                    binding_id=_BINDING_ID,
                    connector_family="newsletter",
                    handlers={_WRITE_CAPABILITY: connector.subscribe},
                    provider_mode="local",
                    provider_name="independent-newsletter",
                    provider_version="qualification-v1",
                    durable_receipts=True,
                ),
            ),
        )

    # Only deployment infrastructure is substituted; worker, planner, approval,
    # authorization guard, dispatcher, repositories and receipt checks are real.
    monkeypatch.setattr(catalog_write_workflows, "build_durable_connector_bundle", factory)
    return instances


async def _submit(client, instance_id, *, write=False):
    response = await browser_request(
        client,
        "POST",
        f"/api/v1/agent-instances/{instance_id}/dry-runs",
        json={
            "input": {
                "request_id": "request.obj05.substitution",
                "source_content": (
                    json.dumps({"version": 1, "command": _COMMAND})
                    if write
                    else "Offline evidence still requires human review."
                ),
            },
            "executionMode": "mock_execute" if write else "dry_run",
        },
    )
    assert response.status_code == 202, response.text
    return response.json()["runId"]


async def _approval(runtime, client, run_id):
    async with runtime.dependencies.unit_of_work() as uow:
        current = await uow.approvals.get_current_authorization_set(run_id)
        assert current is not None
        requests = await uow.approvals.list_current_set(
            run_id,
            current.authorization_set.plan_hash,
            current.authorization_set.proposal_revision,
        )
        assert len(requests) == 1
        request = requests[0].request
    response = await browser_request(
        client,
        "POST",
        f"/api/v1/approvals/{request.id}/approve",
        json={
            "expected_generation": request.generation,
            "expected_payload_hash": request.action_hash,
            "reason": "Review exact local independent adapter command.",
        },
    )
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", (False, True))
async def test_obj_05_independent_llm_uses_worker_and_controlled_read_boundary(
    tmp_path: Path, malformed: bool
) -> None:
    settings = await _installation(tmp_path)
    runtime = await build_runtime(settings, clock=Clock())
    provider = _IndependentStructuredProvider(malformed=malformed)
    try:
        _compose_model(runtime, provider)
        async with AsyncClient(
            transport=ASGITransport(app=runtime.create_app()), base_url="http://testserver"
        ) as client:
            run_id = await _submit(client, _MODEL_INSTANCE)
        assert provider.requests == []  # HTTP admission does not execute a provider.
        assert await RunWorker(runtime, "worker.obj05.model").drain_once()
        assert len(provider.requests) == 1
        request = provider.requests[0]
        assert request.context.run_id == run_id
        assert request.system_instructions.trust_class == "trusted_system"
        assert request.system_instructions.template_id == _MODEL_TEMPLATE
        assert len(request.retrieved_content) == 1 and not request.tool_results
        async with runtime.dependencies.unit_of_work() as uow:
            run = await uow.runs.get(run_id)
            assert run is not None
            control = await uow.execution_control.get(run_id)
            assert control is not None and control.model_calls == 1 and control.tool_calls == 0
            artifacts = await uow.artifacts.list_for_run(run_id)
            steps = await uow.run_steps.validate_plan_for_execution(run_id)
            assert len(steps) == 1
            attempts = await uow.execution_control.list_attempts(
                steps[0].id, steps[0].runtime_policy.operation_key
            )
            assert len(attempts) == 1
            if malformed:
                assert run.state is RunState.FAILED
                assert not artifacts
                assert steps[0].state is StepState.FAILED
                assert attempts[0].safe_error_code == "output_schema_invalid"
                assert attempts[0].output_artifact_id is None
            else:
                assert run.state is RunState.COMPLETED
                assert len(artifacts) == 1 and artifacts[0].verify_payload()
                artifact = artifacts[0]
                assert attempts[0].output_artifact_id == artifact.provenance.artifact_id
                assert artifact.payload["artifact"].startswith("Independent draft:")
                assert artifact.provenance.output_schema_id == request.output_schema_id
                assert artifact.provenance.output_schema_hash == request.output_schema_hash
                assert len(artifact.provenance.providers) == 1
                metadata = artifact.provenance.providers[0]
                assert (metadata.mode, metadata.name, metadata.version) == (
                    "local",
                    "independent-local-model",
                    "qualification-v1",
                )
        assert not await RunWorker(runtime, "worker.obj05.model-replay").drain_once()
        assert len(provider.requests) == 1
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_obj_05_independent_connector_approval_durable_receipt_and_restart_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = await _installation(tmp_path)
    connectors = _inject_newsletter_factory(monkeypatch)
    clock = Clock()
    runtime = await build_runtime(settings, clock=clock)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=runtime.create_app()), base_url="http://testserver"
        ) as client:
            run_id = await _submit(client, _WRITE_INSTANCE, write=True)
            assert await RunWorker(runtime, "worker.obj05.proposal").drain_once()
            assert not connectors[0].calls
            await _approval(runtime, client, run_id)
            assert not connectors[0].calls  # Approval is not delivery.
        await runtime.close()
        clock.current += timedelta(seconds=2)
        runtime = await build_runtime(settings, clock=clock)
        assert await RunWorker(runtime, "worker.obj05.approved-restart").drain_once()
        assert [len(item.calls) for item in connectors] == [0, 1]
        assert [item.inserted_receipts for item in connectors] == [0, 1]
        async with runtime.dependencies.unit_of_work() as uow:
            run = await uow.runs.get(run_id)
            assert run is not None and run.state is RunState.COMPLETED
            plan = await uow.run_steps.get_plan(run_id)
            assert plan is not None
            actions = await uow.external_actions.list_run_plan(run_id, plan.plan_hash)
            assert len(actions) == 1 and actions[0].state is ExternalActionState.SUCCEEDED
            action = actions[0]
            receipt = await uow.connector_receipts.get(_BINDING_ID, action.idempotency_key)
            assert receipt is not None
            assert receipt.external_action_id == action.id
            assert receipt.action_hash == action.action_hash
            assert receipt.capability_id == _WRITE_CAPABILITY
            assert receipt.safe_metadata["provider"] == "independent-newsletter"
            assert action.result is not None and action.result.receipt_id == receipt.receipt_id
            control = await uow.execution_control.get(run_id)
            assert control is not None and control.tool_calls == 1 and control.model_calls == 0
            assert not await uow.artifacts.list_for_run(run_id)
        await runtime.close()
        runtime = await build_runtime(settings, clock=clock)
        await runtime.catalog_writes.resume_persisted(
            run_id, worker_id="worker.obj05.replay", correlation_id="correlation.obj05.replay"
        )
        assert not await RunWorker(runtime, "worker.obj05.completed").drain_once()
        assert [len(item.calls) for item in connectors] == [0, 1, 0]
        assert [item.inserted_receipts for item in connectors] == [0, 1, 0]
        async with runtime.dependencies.unit_of_work() as uow:
            assert await uow.connector_receipts.get(_BINDING_ID, action.idempotency_key) == receipt
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("binding_changed_after_approval", (False, True))
async def test_obj_05_independent_connector_cannot_bypass_approval_or_binding_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, binding_changed_after_approval: bool
) -> None:
    settings = await _installation(tmp_path)
    connectors = _inject_newsletter_factory(monkeypatch)
    clock = Clock()
    runtime = await build_runtime(settings, clock=clock)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=runtime.create_app()), base_url="http://testserver"
        ) as client:
            run_id = await _submit(client, _WRITE_INSTANCE, write=True)
            assert await RunWorker(runtime, "worker.obj05.boundary-prepare").drain_once()
            if binding_changed_after_approval:
                await _approval(runtime, client, run_id)
                path = f"/api/v1/agent-instances/{_WRITE_INSTANCE}/configuration"
                current = await client.get(path)
                assert current.status_code == 200
                changed = await browser_request(
                    client,
                    "PATCH",
                    path,
                    headers={"If-Match": current.headers["etag"]},
                    json={
                        "connectorBindings": {
                            "newsletter": {
                                "connectorFamily": "newsletter",
                                "bindingId": _BINDING_ID,
                                "enabled": False,
                            }
                        }
                    },
                )
                assert changed.status_code == 200, changed.text
                assert changed.json()["configuration"]["configurationRevision"] == 2
                # Restore the same enabled binding before resume. Only its
                # revision differs from the approved snapshot: this isolates
                # revision drift instead of relying on a disabled connector.
                current = await client.get(path)
                restored = await browser_request(
                    client,
                    "PATCH",
                    path,
                    headers={"If-Match": current.headers["etag"]},
                    json={
                        "connectorBindings": {
                            "newsletter": {
                                "connectorFamily": "newsletter",
                                "bindingId": _BINDING_ID,
                                "enabled": True,
                            }
                        }
                    },
                )
                assert restored.status_code == 200, restored.text
                configuration = restored.json()["configuration"]
                assert configuration["configurationRevision"] == 3
                assert configuration["connectorBindings"]["newsletter"]["enabled"] is True
        await runtime.close()
        clock.current += timedelta(seconds=2)
        runtime = await build_runtime(settings, clock=clock)
        assert await RunWorker(runtime, "worker.obj05.boundary-restarted").drain_once()
        assert all(not connector.calls for connector in connectors)
        assert all(connector.inserted_receipts == 0 for connector in connectors)
        async with runtime.dependencies.unit_of_work() as uow:
            run = await uow.runs.get(run_id)
            assert run is not None
            assert run.state is (
                RunState.FAILED if binding_changed_after_approval else RunState.AWAITING_APPROVAL
            )
            control = await uow.execution_control.get(run_id)
            assert control is not None and control.tool_calls == control.model_calls == 0
            plan = await uow.run_steps.get_plan(run_id)
            assert plan is not None
            actions = await uow.external_actions.list_run_plan(run_id, plan.plan_hash)
            assert len(actions) == 1
            assert actions[0].delivery_contract.binding_configuration_revision == 1
            assert await uow.connector_receipts.get(_BINDING_ID, actions[0].idempotency_key) is None
            assert not await uow.artifacts.list_for_run(run_id)
    finally:
        await runtime.close()

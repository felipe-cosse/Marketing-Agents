"""DEL-07: registry READ/WRITE guards retain exact contracts and failure certainty."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from marketing_agents.application.ports.connector_families import SubscribeContactCommand
from marketing_agents.application.ports.connectors import ConnectorPortError, ConnectorWriteResult
from marketing_agents.application.ports.external_writes import ConnectorDeliveryFailure
from marketing_agents.application.ports.read_adapter import (
    ReadAdapterPermanentError,
    ReadAdapterTransientError,
)
from marketing_agents.domain.entities import DeliveryContractSnapshot, ExternalAction
from marketing_agents.domain.enums import Effect
from marketing_agents.infrastructure.adapters.connectors.dispatch import (
    RegistryConnectorReadAdapter,
    RegistryConnectorWriteGateway,
)
from marketing_agents.infrastructure.adapters.connectors.mock.families import MockConnectorBundle
from marketing_agents.infrastructure.adapters.connectors.registry import (
    ConnectorBundleConfigurationError,
    build_connector_registry,
)

from tests.contract.test_arch_07_connector_contract_matrix import (
    CATALOG,
    _authorized_command,
    _read_operation,
    _read_request,
)
from tests.unit.application.test_del_07_approval_decision_faults import _decision_fixture


def _read_fixture() -> SimpleNamespace:
    registry = build_connector_registry(CATALOG)
    original = MockConnectorBundle.create(registry)
    request = _read_request(_read_operation(registry))
    return SimpleNamespace(registry=registry, bundle=original, request=request)


def _read_adapter(fixture: SimpleNamespace, **updates: Any) -> RegistryConnectorReadAdapter:
    bundle = replace(fixture.bundle, **updates)
    return RegistryConnectorReadAdapter(
        fixture.registry, bundle, binding_configuration_revisions={"mock.social.default": 7}
    )


@pytest.mark.parametrize("revision", (0, -1, True, 1.0, "1"))
def test_del_07_read_binding_revision_is_an_exact_positive_integer(revision: object) -> None:
    fixture = _read_fixture()
    with pytest.raises(ConnectorBundleConfigurationError, match="normalized positive"):
        RegistryConnectorReadAdapter(
            fixture.registry,
            fixture.bundle,
            binding_configuration_revisions={"mock.social.default": revision},  # type: ignore[dict-item]
        )


def test_del_07_read_adapter_rejects_foreign_registry_and_unnormalized_bindings() -> None:
    fixture = _read_fixture()
    with pytest.raises(ConnectorBundleConfigurationError, match="exact registry"):
        RegistryConnectorReadAdapter(
            build_connector_registry(CATALOG), fixture.bundle, binding_configuration_revisions={}
        )
    for binding in ("", " mock.social.default"):
        with pytest.raises(ConnectorBundleConfigurationError, match="normalized positive"):
            RegistryConnectorReadAdapter(
                fixture.registry, fixture.bundle, binding_configuration_revisions={binding: 7}
            )


@pytest.mark.parametrize(
    ("fault", "code"),
    (
        ("wrong_type", "adapter_contract_invalid"),
        ("model", "adapter_contract_invalid"),
        ("no_binding", "adapter_contract_invalid"),
        ("write", "adapter_contract_invalid"),
        ("result_type", "adapter_contract_invalid"),
        ("invalid_revision", "adapter_contract_drift"),
    ),
)
def test_del_07_read_contract_rejects_corrupt_or_wrong_kind_registration(
    fault: str, code: str
) -> None:
    fixture = _read_fixture()
    adapter = _read_adapter(fixture)
    operation = _read_operation(fixture.registry)
    registration = fixture.registry.resolve(operation.capability_id)
    if fault == "wrong_type":
        operation = object()
    elif fault == "model":
        object.__setattr__(operation, "connector_family", "model")
    elif fault == "no_binding":
        object.__setattr__(operation, "binding_id", None)
    elif fault == "write":
        adapter._registry = SimpleNamespace(
            resolve=Mock(return_value=fixture.registry.resolve("cap.newsletter.subscribe"))
        )
    elif fault == "result_type":
        adapter._registry = SimpleNamespace(
            resolve=Mock(return_value=replace(registration, result_type=object))
        )
    else:
        adapter._binding_revisions = {"mock.social.default": 0}
    with pytest.raises(ReadAdapterPermanentError) as failure:
        adapter.contract_for(operation)
    assert failure.value.code == code


@pytest.mark.parametrize("direction", ("input", "output"))
def test_del_07_read_schema_contracts_reject_registry_drift_between_lookups(direction: str) -> None:
    fixture = _read_fixture()
    adapter = _read_adapter(fixture)
    operation = _read_operation(fixture.registry)
    registration = fixture.registry.resolve(operation.capability_id)
    # The initial contract check succeeds. A later resolver response must still
    # be validated independently before exposing the schema to the runtime.
    malformed = (
        replace(registration, request_type=ConnectorWriteResult)
        if direction == "input"
        else replace(registration, result_type=object)
    )
    adapter._registry = SimpleNamespace(resolve=Mock(side_effect=(registration, malformed)))
    with pytest.raises(ReadAdapterPermanentError) as failure:
        getattr(adapter, f"{direction}_contract_for")(operation)
    assert failure.value.code == "adapter_contract_invalid"


@pytest.mark.parametrize("fault", ("model", "missing_binding", "missing_registry"))
@pytest.mark.asyncio
async def test_del_07_read_request_resolves_only_external_registered_bindings(fault: str) -> None:
    fixture = _read_fixture()
    adapter = _read_adapter(fixture)
    if fault == "model":
        object.__setattr__(fixture.request.contract, "connector_family", "model")
    elif fault == "missing_binding":
        object.__setattr__(fixture.request.contract, "binding_id", None)
    else:
        adapter._registry = SimpleNamespace(
            resolve=Mock(side_effect=ConnectorBundleConfigurationError("private registry detail"))
        )
    with pytest.raises(ReadAdapterPermanentError) as failure:
        await adapter.execute(fixture.request)
    assert failure.value.code == (
        "adapter_contract_unavailable"
        if fault == "missing_registry"
        else "adapter_contract_invalid"
    )
    assert "private" not in str(failure.value)


def test_del_07_read_observation_cannot_use_an_unregistered_non_model_type() -> None:
    fixture = _read_fixture()
    with pytest.raises(ReadAdapterPermanentError) as failure:
        RegistryConnectorReadAdapter._validate_observation(fixture.request, object, object())
    assert failure.value.code == "adapter_contract_invalid"


def test_del_07_read_input_and_output_contracts_expose_the_registered_schema() -> None:
    fixture = _read_fixture()
    adapter = _read_adapter(fixture)
    operation = _read_operation(fixture.registry)
    input_contract = adapter.input_contract_for(operation)
    output_contract = adapter.output_contract_for(operation)
    assert input_contract.schema_id == operation.request_schema_id
    assert output_contract.schema_id == operation.result_schema_id
    assert "resource_ids" in input_contract.schema["properties"]
    assert "records" in output_contract.schema["properties"]


@pytest.mark.parametrize(
    ("fault", "expected_code", "invocations"),
    (
        ("wrong_request_type", "connector_request_rejected", 0),
        ("missing_method", "adapter_contract_unavailable", 0),
        ("safe_port_failure", "binding_mismatch", 1),
        ("private_port_failure", "connector_request_rejected", 1),
        ("transport_failure", "connector_read_unavailable", 1),
        ("untyped_response", "connector_result_rejected", 1),
    ),
)
@pytest.mark.asyncio
async def test_del_07_read_failures_are_classified_without_implicit_retry(
    fault: str, expected_code: str, invocations: int
) -> None:
    fixture = _read_fixture()
    method = AsyncMock()
    if fault == "safe_port_failure":
        method.side_effect = ConnectorPortError("binding_mismatch", "private provider detail")
    elif fault == "private_port_failure":
        method.side_effect = ConnectorPortError("provider-private-code", "private provider detail")
    elif fault == "transport_failure":
        method.side_effect = RuntimeError("private transport detail")
    else:
        method.return_value = object()
    connector = object() if fault == "missing_method" else SimpleNamespace(read_posts=method)
    adapter = _read_adapter(fixture, social=connector)
    request = object() if fault == "wrong_request_type" else fixture.request
    expected_type = (
        ReadAdapterTransientError if fault == "transport_failure" else ReadAdapterPermanentError
    )
    with pytest.raises(expected_type) as failure:
        await adapter.execute(request)
    assert failure.value.code == expected_code
    assert "private" not in str(failure.value)
    assert method.await_count == invocations


def _write_fixture() -> SimpleNamespace:
    registry = build_connector_registry(CATALOG)
    original = MockConnectorBundle.create(registry)
    authorization = _authorized_command(
        original,
        "cap.newsletter.subscribe",
        SubscribeContactCommand(contact_ref="contact:del07", list_ref="list:del07"),
    ).authorization
    # The gateway only needs the ledger's durable capability. This test double
    # has no execution method and cannot produce a side effect itself.
    bundle = replace(original, ledger=SimpleNamespace(durable=True))
    registration = registry.resolve(authorization.action.capability_id)
    return SimpleNamespace(
        registry=registry,
        bundle=bundle,
        authorization=authorization,
        registration=registration,
    )


@pytest.mark.parametrize(
    ("fault", "expected_code", "may_have_left", "invocations"),
    (
        ("resolution", "connector_request_rejected", False, 0),
        ("invalid_command", "connector_request_rejected", False, 0),
        ("missing_method", "connector_request_rejected", False, 0),
        ("safe_port_failure", "binding_mismatch", False, 1),
        ("private_port_failure", "connector_request_rejected", False, 1),
        ("delivery_exception", "connector_delivery_uncertain", True, 1),
        ("wrong_response", "schema_invalid_response", True, 1),
        ("malformed_response", "schema_invalid_response", True, 1),
    ),
)
@pytest.mark.asyncio
async def test_del_07_write_failures_preserve_pre_call_vs_ambiguous_delivery_boundary(
    fault: str, expected_code: str, may_have_left: bool, invocations: int
) -> None:
    fixture = _write_fixture()
    method = AsyncMock()
    if fault == "safe_port_failure":
        method.side_effect = ConnectorPortError("binding_mismatch", "private provider detail")
    elif fault == "private_port_failure":
        method.side_effect = ConnectorPortError("provider-private-code", "private provider detail")
    elif fault == "delivery_exception":
        method.side_effect = RuntimeError("private transport detail")
    elif fault == "malformed_response":
        method.return_value = ConnectorWriteResult.model_construct(
            receipt_id="receipt:del07", status="mock_succeeded", safe_metadata={"bad": object()}
        )
    else:
        method.return_value = object()
    registration = fixture.registration
    if fault == "invalid_command":
        registration = replace(registration, request_type=type(fixture.bundle))
    connector = object() if fault == "missing_method" else SimpleNamespace(subscribe=method)
    bundle = replace(fixture.bundle, newsletter=connector)
    registry = SimpleNamespace(resolve=Mock(return_value=registration))
    if fault == "resolution":
        registry.resolve.side_effect = ConnectorBundleConfigurationError("private registry detail")
    gateway = RegistryConnectorWriteGateway(
        registry, bundle, binding_configuration_revisions={"mock.newsletter.default": 1}
    )
    with pytest.raises(ConnectorDeliveryFailure) as failure:
        await gateway.execute(fixture.authorization)
    assert failure.value.code == expected_code
    assert failure.value.request_may_have_left_process is may_have_left
    assert "private" not in str(failure.value)
    assert method.await_count == invocations


def test_del_07_write_contract_rejects_missing_binding_and_read_capability() -> None:
    fixture = _write_fixture()
    action = _decision_fixture().action
    gateway = RegistryConnectorWriteGateway(
        fixture.registry, fixture.bundle, binding_configuration_revisions={}
    )
    with pytest.raises(ConnectorDeliveryFailure) as unavailable:
        gateway.contract_for(action)
    assert unavailable.value.code == "delivery_contract_unavailable"
    registration = replace(
        fixture.registration, metadata=replace(fixture.registration.metadata, effect=Effect.READ)
    )
    gateway = RegistryConnectorWriteGateway(
        SimpleNamespace(resolve=Mock(return_value=registration)),
        fixture.bundle,
        binding_configuration_revisions={action.connector_binding_id: 1},
    )
    with pytest.raises(ConnectorDeliveryFailure) as read:
        gateway.contract_for(action)
    assert read.value.code == "delivery_effect_mismatch"
    assert not unavailable.value.request_may_have_left_process
    assert not read.value.request_may_have_left_process


def test_del_07_write_gateway_rejects_a_non_durable_ledger() -> None:
    fixture = _write_fixture()
    with pytest.raises(ConnectorBundleConfigurationError, match="durable"):
        RegistryConnectorWriteGateway(
            fixture.registry,
            replace(fixture.bundle, ledger=SimpleNamespace(durable=False)),
            binding_configuration_revisions={},
        )


@pytest.mark.asyncio
async def test_del_07_write_success_preserves_exact_validated_receipt_without_retry() -> None:
    fixture = _write_fixture()
    result = ConnectorWriteResult(
        receipt_id="receipt:del07",
        status="mock_succeeded",
        safe_metadata={"external_side_effect": False},
    )
    method = AsyncMock(return_value=result)
    gateway = RegistryConnectorWriteGateway(
        fixture.registry,
        replace(fixture.bundle, newsletter=SimpleNamespace(subscribe=method)),
        binding_configuration_revisions={"mock.newsletter.default": 1},
    )
    assert await gateway.execute(fixture.authorization) == result
    method.assert_awaited_once()
    assert method.await_args.args[0].authorization is fixture.authorization


def test_del_07_write_contract_compares_persisted_revision_idempotency_and_timeout() -> None:
    fixture = _write_fixture()
    metadata = fixture.registration.metadata
    envelope = fixture.authorization.action
    prototype = _decision_fixture().action
    # A valid planned write with the registered schema, not a forged proof.
    from marketing_agents.domain.approval import ProposedExternalAction

    proposal = ProposedExternalAction.create(
        envelope,
        redacted_destination="configured list",
        payload_schema={"type": "object"},
    )
    contract = DeliveryContractSnapshot(
        metadata.capability_id,
        metadata.connector_family,
        envelope.binding_id,
        1,
        metadata.request_schema_id,
        "required",
        metadata.default_timeout_seconds,
    )
    action = ExternalAction.proposed(
        proposal, prototype.approval_policy, contract, prototype.created_at
    )
    gateway = RegistryConnectorWriteGateway(
        fixture.registry,
        fixture.bundle,
        binding_configuration_revisions={action.connector_binding_id: 1},
    )
    assert gateway.contract_for(action).capability_id == envelope.capability_id
    for field, changed in (
        ("binding_configuration_revision", 2),
        ("idempotency_support", "unavailable"),
        ("timeout_seconds", 1),
    ):
        drifted = replace(action, delivery_contract=replace(contract, **{field: changed}))
        with pytest.raises(ConnectorDeliveryFailure) as failure:
            gateway.contract_for(drifted)
        assert failure.value.code == "delivery_contract_drift"
        assert failure.value.request_may_have_left_process is False

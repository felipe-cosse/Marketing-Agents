"""OBJ-05: implementation-neutral connector binding and typed extension contracts."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace
from typing import Any

import pytest
from marketing_agents.application.ports.connector_families import (
    CommentsPayload,
    PostsPayload,
    ReadCommentsRequest,
    ReadPostsRequest,
    SubscribeContactCommand,
    UnsubscribeContactCommand,
)
from marketing_agents.application.ports.connectors import (
    AuthorizedConnectorCommand,
    ConnectorObservation,
    ConnectorWriteResult,
)
from marketing_agents.application.ports.external_writes import ConnectorDeliveryFailure
from marketing_agents.application.ports.read_adapter import ReadAdapterPermanentError
from marketing_agents.domain.data_classification import DataClassification
from marketing_agents.domain.schema_hash import canonical_schema_hash
from marketing_agents.infrastructure.adapters.connectors.bindings import (
    ConnectorBindingRegistration,
    ConnectorBindingRegistry,
)
from marketing_agents.infrastructure.adapters.connectors.dispatch import (
    RegistryConnectorReadAdapter,
    RegistryConnectorWriteGateway,
)
from marketing_agents.infrastructure.adapters.connectors.mock.families import MockConnectorBundle
from marketing_agents.infrastructure.adapters.connectors.registry import (
    OPERATION_REGISTRATIONS,
    ConnectorBundleConfigurationError,
    ConnectorOperationRegistry,
    build_connector_registry,
)

from tests.contract.test_arch_07_connector_contract_matrix import (
    CATALOG,
    _authorized_command,
    _read_operation,
    _read_request,
)


class _IndependentSocial:
    """An independent in-process adapter, not a shipped-mock subclass or wrapper."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[ReadPostsRequest] = []

    async def read_posts(self, request: ReadPostsRequest) -> ConnectorObservation[PostsPayload]:
        assert type(request) is ReadPostsRequest
        self.calls.append(request)
        return ConnectorObservation[PostsPayload](
            capability_id=request.capability_id,
            binding_id=request.context.binding_id,
            observation_id=f"observation:obj-05:{self.name}",
            payload=PostsPayload(records=()),
            provenance_ids=request.context.provenance_ids,
            classification=DataClassification.INTERNAL,
        )


class _IndependentNewsletter:
    """A gateway-only fixture; durable receipt behavior is tested in integration."""

    def __init__(self) -> None:
        self.calls: list[AuthorizedConnectorCommand[SubscribeContactCommand]] = []

    async def subscribe(
        self, request: AuthorizedConnectorCommand[SubscribeContactCommand]
    ) -> ConnectorWriteResult:
        assert type(request.command) is SubscribeContactCommand
        self.calls.append(request)
        return ConnectorWriteResult(receipt_id="receipt:obj-05", status="local_succeeded")


def _social_binding(
    handler: _IndependentSocial, binding_id: str = "local.social.first", **changes: Any
) -> ConnectorBindingRegistration:
    values: dict[str, Any] = {
        "binding_id": binding_id,
        "connector_family": "social",
        "handlers": {"cap.social.read-posts": handler.read_posts},
        "provider_mode": "local",
        "provider_name": handler.name,
        "provider_version": "adapter-v3",
    }
    return ConnectorBindingRegistration(**(values | changes))


def _newsletter_binding(
    handler: _IndependentNewsletter, **changes: Any
) -> ConnectorBindingRegistration:
    values: dict[str, Any] = {
        "binding_id": "mock.newsletter.default",
        "connector_family": "newsletter",
        "handlers": {"cap.newsletter.subscribe": handler.subscribe},
        "provider_mode": "local",
        "provider_name": "independent-newsletter",
        "provider_version": "adapter-v2",
        "durable_receipts": True,
    }
    return ConnectorBindingRegistration(**(values | changes))


@pytest.mark.parametrize(
    ("capability_id", "field", "replacement"),
    (
        ("cap.social.read-posts", "request_type", object),
        ("cap.social.read-posts", "request_type", ReadCommentsRequest),
        ("cap.social.read-posts", "request_type", SubscribeContactCommand),
        ("cap.newsletter.subscribe", "request_type", ReadPostsRequest),
        ("cap.newsletter.subscribe", "request_type", UnsubscribeContactCommand),
        ("cap.social.read-posts", "result_type", object),
        ("cap.social.read-posts", "result_type", CommentsPayload),
        ("cap.social.read-posts", "result_type", ConnectorWriteResult),
        ("cap.newsletter.subscribe", "result_type", CommentsPayload),
        ("cap.social.read-posts", "method_name", "read_comments"),
        ("cap.newsletter.subscribe", "method_name", "unsubscribe"),
    ),
)
def test_obj_05_catalog_rejects_incompatible_typed_operation_declarations(
    capability_id: str, field: str, replacement: object
) -> None:
    registrations = tuple(
        replace(registration, **{field: replacement})
        if registration.metadata.capability_id == capability_id
        else registration
        for registration in OPERATION_REGISTRATIONS
    )
    registry = ConnectorOperationRegistry(registrations)
    with pytest.raises(
        ConnectorBundleConfigurationError, match="connector typed operation contract drift"
    ):
        registry.validate_catalog(CATALOG)


@pytest.mark.asyncio
async def test_obj_05_same_family_bindings_select_exact_handlers_and_provider_metadata() -> None:
    registry = build_connector_registry(CATALOG)
    first = _IndependentSocial("first-provider")
    second = _IndependentSocial("second-provider")
    bindings = ConnectorBindingRegistry(
        registry,
        (
            _social_binding(first),
            _social_binding(second, "local.social.second", provider_version="adapter-v9"),
        ),
    )
    adapter = RegistryConnectorReadAdapter(
        registry,
        bindings,
        binding_configuration_revisions={"local.social.first": 7, "local.social.second": 7},
    )

    for name, handler, version in (
        ("first", first, "adapter-v3"),
        ("second", second, "adapter-v9"),
    ):
        operation = replace(_read_operation(registry), binding_id=f"local.social.{name}")
        assert adapter.contract_for(operation).binding_id == operation.binding_id
        output = adapter.output_contract_for(operation)
        assert (output.provider_mode, output.provider_name, output.provider_version) == (
            "local",
            handler.name,
            version,
        )
        request = _read_request(operation)
        result = await adapter.execute(request)
        assert result.observation_id == f"observation:obj-05:{handler.name}"
        assert result.binding_id == operation.binding_id
        assert result.provenance_ids == request.provenance_ids
        assert result.trust_class == "untrusted_tool_result"

    assert [request.context.binding_id for request in first.calls] == ["local.social.first"]
    assert [request.context.binding_id for request in second.calls] == ["local.social.second"]


@pytest.mark.asyncio
async def test_obj_05_binding_and_revision_snapshots_are_detached_and_immutable() -> None:
    registry = build_connector_registry(CATALOG)
    handler = _IndependentSocial("immutable")
    handlers = {"cap.social.read-posts": handler.read_posts}
    versions = {"cap.social.read-posts": "operation-v4"}
    registration = _social_binding(handler, handlers=handlers, operation_provider_versions=versions)
    bindings = ConnectorBindingRegistry(registry, (registration,))
    revisions = {registration.binding_id: 7}
    adapter = RegistryConnectorReadAdapter(
        registry, bindings, binding_configuration_revisions=revisions
    )
    handlers.clear()
    versions.clear()
    revisions.clear()

    assert bindings.binding_registry is bindings
    assert bindings.registry is registry
    assert tuple(bindings.binding_ids) == (registration.binding_id,)
    assert bindings.bindings == (registration,)
    assert bindings.resolve(registration.binding_id) is registration
    with pytest.raises(TypeError):
        registration.handlers["cap.social.read-posts"] = object()  # type: ignore[index]
    with pytest.raises(TypeError):
        registration.operation_provider_versions["cap.social.read-posts"] = "changed"  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        registration.provider_name = "changed"  # type: ignore[misc]

    operation = replace(_read_operation(registry), binding_id=registration.binding_id)
    assert adapter.output_contract_for(operation).provider_version == "operation-v4"
    assert adapter.contract_for(operation).binding_configuration_revision == 7
    await adapter.execute(_read_request(operation))
    assert len(handler.calls) == 1


def test_obj_05_binding_registry_rejects_duplicate_and_unknown_binding_ids() -> None:
    registry = build_connector_registry(CATALOG)
    handler = _IndependentSocial("duplicate")
    registration = _social_binding(handler)
    with pytest.raises(ConnectorBundleConfigurationError):
        ConnectorBindingRegistry(registry, (registration, registration))
    bindings = ConnectorBindingRegistry(registry, (registration,))
    with pytest.raises(ConnectorBundleConfigurationError):
        bindings.resolve("local.social.missing")
    with pytest.raises(ConnectorBundleConfigurationError):
        RegistryConnectorReadAdapter(
            registry, bindings, binding_configuration_revisions={"local.social.missing": 7}
        )
    assert handler.calls == []


@pytest.mark.parametrize(
    "changes",
    (
        {"binding_id": ""},
        {"binding_id": " local.social.first"},
        {"connector_family": "invented-family"},
        {"provider_mode": "invented-mode"},
        {"provider_name": ""},
        {"provider_version": ""},
        {"durable_receipts": 1},
        {"handlers": {}},
        {"operation_provider_versions": {"cap.social.read-comments": "undeclared-v1"}},
    ),
)
def test_obj_05_binding_metadata_is_exact_and_complete(changes: dict[str, Any]) -> None:
    handler = _IndependentSocial("invalid-binding")
    with pytest.raises(ConnectorBundleConfigurationError):
        ConnectorBindingRegistry(
            build_connector_registry(CATALOG), (_social_binding(handler, **changes),)
        )
    assert handler.calls == []


@pytest.mark.parametrize(
    "capability_id",
    ("cap.social.unknown", "cap.newsletter.subscribe", "cap.email.send-message"),
)
def test_obj_05_bindings_reject_unknown_cross_family_and_disabled_capabilities(
    capability_id: str,
) -> None:
    handler = _IndependentSocial("invalid-capability")
    family = "newsletter" if capability_id == "cap.email.send-message" else "social"
    with pytest.raises(ConnectorBundleConfigurationError):
        ConnectorBindingRegistry(
            build_connector_registry(CATALOG),
            (
                _social_binding(
                    handler,
                    connector_family=family,
                    handlers={capability_id: handler.read_posts},
                ),
            ),
        )
    assert handler.calls == []


@pytest.mark.parametrize("handler", (None, object(), lambda request: None))
def test_obj_05_binding_handlers_must_be_available_async_functions(handler: object) -> None:
    with pytest.raises(ConnectorBundleConfigurationError):
        ConnectorBindingRegistry(
            build_connector_registry(CATALOG),
            (
                _social_binding(
                    _IndependentSocial("nonasync"),
                    handlers={"cap.social.read-posts": handler},
                ),
            ),
        )


@pytest.mark.parametrize(
    "gateway_type", (RegistryConnectorReadAdapter, RegistryConnectorWriteGateway)
)
@pytest.mark.parametrize("revision", (0, -1, True, 1.0, "1"))
def test_obj_05_both_bridges_reject_nonexact_positive_revisions(
    gateway_type: type, revision: object
) -> None:
    registry = build_connector_registry(CATALOG)
    handler = _IndependentSocial("revision")
    bindings = ConnectorBindingRegistry(registry, (_social_binding(handler),))
    with pytest.raises(ConnectorBundleConfigurationError, match="normalized positive"):
        gateway_type(
            registry,
            bindings,
            binding_configuration_revisions={"local.social.first": revision},
        )
    assert handler.calls == []


@pytest.mark.parametrize(
    "gateway_type", (RegistryConnectorReadAdapter, RegistryConnectorWriteGateway)
)
def test_obj_05_both_bridges_require_the_binding_sources_exact_operation_registry(
    gateway_type: type,
) -> None:
    registry = build_connector_registry(CATALOG)
    handler = _IndependentSocial("registry")
    bindings = ConnectorBindingRegistry(registry, (_social_binding(handler),))
    with pytest.raises(ConnectorBundleConfigurationError, match="exact registry"):
        gateway_type(
            build_connector_registry(CATALOG),
            bindings,
            binding_configuration_revisions={"local.social.first": 7},
        )
    assert handler.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ("unknown_binding", "wrong_family", "unbound_capability", "revision")
)
async def test_obj_05_read_contract_drift_or_unbound_operations_never_call(fault: str) -> None:
    registry = build_connector_registry(CATALOG)
    handler = _IndependentSocial("pre-call")
    binding = _social_binding(handler)
    bindings = ConnectorBindingRegistry(registry, (binding,))
    adapter = RegistryConnectorReadAdapter(
        registry, bindings, binding_configuration_revisions={binding.binding_id: 7}
    )
    operation = replace(_read_operation(registry), binding_id=binding.binding_id)
    if fault == "unknown_binding":
        operation = replace(operation, binding_id="local.social.missing")
    elif fault == "wrong_family":
        operation = replace(operation, connector_family="crm")
    elif fault == "unbound_capability":
        registration = registry.resolve("cap.social.read-comments")
        metadata = registration.metadata
        operation = replace(
            operation,
            capability_id=metadata.capability_id,
            request_schema_id=metadata.request_schema_id,
            result_schema_id=metadata.result_schema_id,
            result_schema_hash=canonical_schema_hash(registration.result_type.model_json_schema()),
            request_redaction_fields=metadata.request_redaction_fields,
            result_redaction_fields=metadata.result_redaction_fields,
            data_classification=metadata.data_classification,
        )
    else:
        operation = replace(operation, configuration_revision=8, binding_configuration_revision=8)
    with pytest.raises(ReadAdapterPermanentError) as failure:
        await adapter.execute(_read_request(operation))
    assert failure.value.code == (
        "adapter_contract_unavailable"
        if fault in {"unknown_binding", "unbound_capability"}
        else "adapter_contract_drift"
    )
    assert handler.calls == []


def test_obj_05_write_dispatch_requires_declared_durable_receipts_before_calls() -> None:
    registry = build_connector_registry(CATALOG)
    handler = _IndependentNewsletter()
    bindings = ConnectorBindingRegistry(
        registry, (_newsletter_binding(handler, durable_receipts=False),)
    )
    for revisions in ({}, {"mock.newsletter.default": 1}):
        with pytest.raises(ConnectorBundleConfigurationError, match="durable"):
            RegistryConnectorWriteGateway(
                registry, bindings, binding_configuration_revisions=revisions
            )
    assert handler.calls == []


@pytest.mark.asyncio
async def test_obj_05_write_gateway_invokes_only_the_exact_binding_without_a_mock_ledger() -> None:
    registry = build_connector_registry(CATALOG)
    chosen = _IndependentNewsletter()
    other = _IndependentNewsletter()
    bindings = ConnectorBindingRegistry(
        registry,
        (
            _newsletter_binding(chosen),
            _newsletter_binding(other, binding_id="local.newsletter.other"),
        ),
    )
    gateway = RegistryConnectorWriteGateway(
        registry,
        bindings,
        binding_configuration_revisions={"mock.newsletter.default": 1, "local.newsletter.other": 1},
    )
    command = SubscribeContactCommand(contact_ref="contact:obj-05", list_ref="list:obj-05")
    authorization = _authorized_command(
        SimpleNamespace(registry=registry), "cap.newsletter.subscribe", command
    ).authorization
    result = await gateway.execute(authorization)
    assert result.receipt_id == "receipt:obj-05"
    assert len(chosen.calls) == 1
    assert chosen.calls[0].authorization is authorization
    assert chosen.calls[0].command == command
    assert other.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ("unknown_binding", "unconfigured_binding"))
async def test_obj_05_unconfigured_write_binding_never_calls_an_available_family_handler(
    fault: str,
) -> None:
    registry = build_connector_registry(CATALOG)
    handler = _IndependentNewsletter()
    binding_id = (
        "local.newsletter.other" if fault == "unknown_binding" else "mock.newsletter.default"
    )
    bindings = ConnectorBindingRegistry(
        registry, (_newsletter_binding(handler, binding_id=binding_id),)
    )
    gateway = RegistryConnectorWriteGateway(
        registry,
        bindings,
        binding_configuration_revisions={binding_id: 1} if fault == "unknown_binding" else {},
    )
    authorization = _authorized_command(
        SimpleNamespace(registry=registry),
        "cap.newsletter.subscribe",
        SubscribeContactCommand(contact_ref="contact:obj-05", list_ref="list:obj-05"),
    ).authorization
    with pytest.raises(ConnectorDeliveryFailure) as failure:
        await gateway.execute(authorization)
    assert failure.value.code == "connector_request_rejected"
    assert failure.value.request_may_have_left_process is False
    assert handler.calls == []


def test_obj_05_default_mock_binding_keeps_its_existing_provenance() -> None:
    registry = build_connector_registry(CATALOG)
    bundle = MockConnectorBundle.create(registry)
    adapter = RegistryConnectorReadAdapter(
        registry, bundle, binding_configuration_revisions={"mock.social.default": 7}
    )
    contract = adapter.output_contract_for(_read_operation(registry))
    assert (contract.provider_mode, contract.provider_name, contract.provider_version) == (
        "mock",
        "social",
        "PostsPayload",
    )

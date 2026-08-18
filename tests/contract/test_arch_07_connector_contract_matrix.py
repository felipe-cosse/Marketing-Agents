"""ARCH-07: typed deterministic mocks cover the exact eight connector families."""

from __future__ import annotations

import ast
import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from marketing_agents.application.policies.write_authorization import (
    ApprovalReservation,
    WriteAuthorizationGuard,
)
from marketing_agents.application.ports.connector_families import (
    CmsConnector,
    ExplicitIds,
    FulfillmentConnector,
    NewsletterConnector,
    PostsPayload,
    ReadContentRequest,
    ReadCustomerRequest,
    ReadFulfillmentStatusRequest,
    ReadMembershipRequest,
    ReadPostsRequest,
    ReadRangeRequest,
    ReadSessionsRequest,
    SendEmailCommand,
    SocialConnector,
    SpreadsheetRangeParameters,
    SubscribeContactCommand,
)
from marketing_agents.application.ports.connectors import (
    AuthorizedConnectorCommand,
    ConnectorCallContext,
    ConnectorPortError,
)
from marketing_agents.config import Settings
from marketing_agents.domain.action_hash import CanonicalExternalAction, canonical_action_hash
from marketing_agents.infrastructure.adapters.connectors import mock, registry
from marketing_agents.infrastructure.adapters.connectors.mock import build_connector_bundle
from marketing_agents.infrastructure.adapters.connectors.registry import (
    DISABLED_V1_CAPABILITIES,
    EXTERNAL_CONNECTOR_FAMILIES,
    ConnectorBundleConfigurationError,
    ConnectorOperationRegistry,
    build_connector_registry,
)
from marketing_agents.infrastructure.catalog import compile_catalog

ROOT = Path(__file__).resolve().parents[2]
CATALOG = compile_catalog(ROOT / "catalog" / "v1")
IDEMPOTENCY_KEY = "arch-07-idempotency-key-0001"


def _context(family: str, *, run_id: str = "run:arch-07") -> ConnectorCallContext:
    return ConnectorCallContext(
        binding_id=f"mock.{family}.default",
        run_id=run_id,
        step_id="step:connector",
        correlation_id=f"correlation:{run_id}",
        deadline=datetime.now(UTC) + timedelta(minutes=1),
        provenance_ids=("work-input:arch-07",),
        requested_timeout_seconds=30,
    )


def _authorized_command(
    bundle: object,
    capability_id: str,
    command: SubscribeContactCommand | SendEmailCommand,
) -> (
    AuthorizedConnectorCommand[SubscribeContactCommand]
    | AuthorizedConnectorCommand[SendEmailCommand]
):
    registration = bundle.registry.declaration(capability_id)  # type: ignore[attr-defined]
    metadata = registration.metadata
    action = CanonicalExternalAction(
        action_id=f"action:{capability_id}",
        authorization_set_id="authorization-set:arch-07",
        run_id="run:arch-07",
        step_id="step:connector-write",
        template_id="tpl.email.newsletter.newsletter-subscriber",
        instance_id="inst.email.newsletter.newsletter-subscriber.01",
        action_type=capability_id.removeprefix("cap."),
        capability_id=capability_id,
        connector_family=metadata.connector_family,
        binding_id=f"mock.{metadata.connector_family}.default",
        destination="mock-destination:arch-07",
        payload_schema_id=metadata.request_schema_id,
        minimized_payload=command.model_dump(mode="json"),
    )
    reservation = ApprovalReservation(
        reservation_id=f"reservation:{capability_id}",
        authorization_set_id=action.authorization_set_id,
        state="dispatch_reserved",
        action_id=action.action_id,
        action_hash=canonical_action_hash(action),
        capability_id=action.capability_id,
        binding_id=action.binding_id,
        approval_request_id=f"approval-request:{capability_id}",
        approval_decision_id=f"approval-decision:{capability_id}",
        idempotency_key=IDEMPOTENCY_KEY,
        reserved_at=datetime.now(UTC),
    )
    authorization = WriteAuthorizationGuard().authorize(action, reservation, IDEMPOTENCY_KEY)
    if isinstance(command, SubscribeContactCommand):
        return AuthorizedConnectorCommand(authorization=authorization, command=command)
    return AuthorizedConnectorCommand(authorization=authorization, command=command)


def test_arch_07_registry_matches_catalog_contract_for_all_eight_families() -> None:
    operation_registry = build_connector_registry(CATALOG)
    catalog_external = {
        item.id: item
        for item in CATALOG.tool_capabilities
        if item.connector_family in EXTERNAL_CONNECTOR_FAMILIES
    }

    assert len(operation_registry.operations) == 20
    assert set(operation_registry.capability_ids) == set(catalog_external)
    assert {item.metadata.connector_family for item in operation_registry.operations} == set(
        EXTERNAL_CONNECTOR_FAMILIES
    )
    assert {
        item.metadata.capability_id
        for item in operation_registry.operations
        if not item.metadata.enabled
    } == set(DISABLED_V1_CAPABILITIES)
    assert sum(item.metadata.enabled for item in operation_registry.operations) == 18

    for operation in operation_registry.operations:
        metadata = operation.metadata
        catalog_item = catalog_external[metadata.capability_id]
        assert metadata.effect.value == catalog_item.effect
        assert metadata.idempotency_support == catalog_item.idempotency_support
        assert metadata.default_timeout_seconds == catalog_item.default_timeout_seconds
        assert metadata.data_classification.value == catalog_item.data_classification
        assert metadata.request_schema_id.endswith(":request:v1")
        assert metadata.result_schema_id.endswith(":result:v1")
        if catalog_item.data_classification == "personal":
            assert metadata.request_redaction_fields or metadata.result_redaction_fields


def test_arch_07_registry_rejects_duplicate_and_drifted_operations() -> None:
    original = registry.OPERATION_REGISTRATIONS[0]
    with pytest.raises(ConnectorBundleConfigurationError, match="duplicate"):
        ConnectorOperationRegistry((original, original))

    drifted = replace(
        original,
        metadata=replace(original.metadata, default_timeout_seconds=29),
    )
    drifted_registry = ConnectorOperationRegistry((drifted, *registry.OPERATION_REGISTRATIONS[1:]))
    with pytest.raises(ConnectorBundleConfigurationError, match="metadata drift"):
        drifted_registry.validate_catalog(CATALOG)


def test_arch_07_each_family_exposes_a_typed_deterministic_mock() -> None:
    bundle = build_connector_bundle(Settings(_env_file=None), CATALOG)
    assert isinstance(bundle.social, SocialConnector)
    assert isinstance(bundle.newsletter, NewsletterConnector)

    social_request = ReadPostsRequest(
        context=_context("social", run_id="run:first"),
        parameters=ExplicitIds(resource_ids=("post:1",)),
    )
    first = asyncio.run(bundle.social.read_posts(social_request))
    second = asyncio.run(
        bundle.social.read_posts(
            social_request.model_copy(update={"context": _context("social", run_id="run:second")})
        )
    )
    assert isinstance(first.payload, PostsPayload)
    assert first.payload == second.payload
    assert first.observation_id == second.observation_id
    assert first.trust_class == "untrusted_tool_result"
    assert first.as_untrusted_tool_result().trust_class == "untrusted_tool_result"

    reads = (
        bundle.crm.read_customer(
            ReadCustomerRequest(
                context=_context("crm"), parameters=ExplicitIds(resource_ids=("customer:1",))
            )
        ),
        bundle.cms.read_content(
            ReadContentRequest(
                context=_context("cms"), parameters=ExplicitIds(resource_ids=("content:1",))
            )
        ),
        bundle.events.read_sessions(
            ReadSessionsRequest(
                context=_context("events"), parameters=ExplicitIds(resource_ids=("session:1",))
            )
        ),
        bundle.community.read_membership(
            ReadMembershipRequest(
                context=_context("community"),
                parameters=ExplicitIds(resource_ids=("member:1",)),
            )
        ),
        bundle.spreadsheet.read_range(
            ReadRangeRequest(
                context=_context("spreadsheet"),
                parameters=SpreadsheetRangeParameters(document_ref="sheet:1", range_a1="A1:B2"),
            )
        ),
        bundle.fulfillment.read_status(
            ReadFulfillmentStatusRequest(
                context=_context("fulfillment"),
                parameters=ExplicitIds(resource_ids=("fulfillment:1",)),
            )
        ),
    )
    observations = asyncio.run(_gather(reads))
    assert len(observations) == 6
    assert all(item.trust_class == "untrusted_tool_result" for item in observations)

    subscribe = SubscribeContactCommand(contact_ref="contact:1", list_ref="list:1")
    write = _authorized_command(bundle, "cap.newsletter.subscribe", subscribe)
    assert isinstance(write, AuthorizedConnectorCommand)
    receipt = asyncio.run(bundle.newsletter.subscribe(write))  # type: ignore[arg-type]
    assert receipt.status == "mock_succeeded"
    assert receipt.safe_metadata["external_side_effect"] is False


async def _gather(awaitables: tuple[object, ...]) -> tuple[object, ...]:
    return tuple(await asyncio.gather(*awaitables))  # type: ignore[arg-type, misc]


def test_arch_07_writes_require_exact_proof_and_disabled_ops_never_call() -> None:
    bundle = build_connector_bundle(Settings(_env_file=None), CATALOG)
    command = SubscribeContactCommand(contact_ref="contact:1", list_ref="list:1")
    write = _authorized_command(bundle, "cap.newsletter.subscribe", command)
    first = asyncio.run(bundle.newsletter.subscribe(write))  # type: ignore[arg-type]
    duplicate = asyncio.run(bundle.newsletter.subscribe(write))  # type: ignore[arg-type]
    assert first == duplicate
    assert bundle.ledger.side_effect_count == 1

    tampered = AuthorizedConnectorCommand(
        authorization=write.authorization,
        command=SubscribeContactCommand(contact_ref="contact:changed", list_ref="list:1"),
    )
    with pytest.raises(ConnectorPortError) as mismatch:
        asyncio.run(bundle.newsletter.subscribe(tampered))
    assert mismatch.value.code == "authorization_mismatch"
    assert bundle.ledger.side_effect_count == 1

    disabled_command = SendEmailCommand(
        contact_ref="contact:1", subject="Mock only", body="Never dispatched."
    )
    disabled_write = _authorized_command(bundle, "cap.email.send-message", disabled_command)
    with pytest.raises(ConnectorPortError) as disabled:
        asyncio.run(bundle.newsletter.send_message(disabled_write))  # type: ignore[arg-type]
    assert disabled.value.code == "operation_disabled"
    assert bundle.ledger.side_effect_count == 1


def test_arch_07_reserved_forbidden_and_real_modes_have_no_handler_or_fallback() -> None:
    operation_registry = build_connector_registry(CATALOG)
    forbidden = {
        "cap.social.publish",
        "cap.cms.update-content",
        "cap.fulfillment.create",
        "cap.generic-http.fetch",
        "cap.browser.navigate",
        "cap.shell.execute",
    }
    assert forbidden.isdisjoint(operation_registry.capability_ids)
    assert not hasattr(CmsConnector, "update_content")
    assert not hasattr(FulfillmentConnector, "create")

    real_settings = Settings(
        _env_file=None,
        connector_mode="real",
        allow_external_network=True,
        real_connector_opt_in=True,
    )
    with pytest.raises(ConnectorBundleConfigurationError, match="not explicitly registered"):
        build_connector_bundle(real_settings, CATALOG)


def test_arch_07_mock_modules_have_no_network_or_sdk_imports() -> None:
    forbidden_roots = {
        "aiohttp",
        "boto3",
        "httpx",
        "requests",
        "socket",
        "urllib",
        "urllib3",
    }
    package_root = Path(mock.__file__ or "").parent
    for source_path in (*package_root.rglob("*.py"), Path(registry.__file__ or "")):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        imported_roots = {
            alias.name.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            (node.module or "").split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        assert imported_roots.isdisjoint(forbidden_roots)

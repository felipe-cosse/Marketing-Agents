"""Eight thin deterministic mock connectors and their fail-closed bundle factory."""

from __future__ import annotations

from dataclasses import dataclass

from marketing_agents.application.ports.connector_families import (
    AttendancePayload,
    CommentsPayload,
    ContentMetadataPayload,
    CourseProgressPayload,
    CustomerPayload,
    EnrollAttendeeCommand,
    FulfillmentStatusPayload,
    MembershipPayload,
    MetricsPayload,
    PostsPayload,
    ProfilesPayload,
    ReadAttendanceRequest,
    ReadCommentsRequest,
    ReadContentRequest,
    ReadCourseProgressRequest,
    ReadCustomerRequest,
    ReadFulfillmentStatusRequest,
    ReadMembershipRequest,
    ReadMetricsRequest,
    ReadPostsRequest,
    ReadProfilesRequest,
    ReadRangeRequest,
    ReadSessionsRequest,
    SendCommunityMessageCommand,
    SendEmailCommand,
    SessionsPayload,
    ShareMaterialCommand,
    SpreadsheetRangePayload,
    SubscribeContactCommand,
    UnsubscribeContactCommand,
    UpdateRowsCommand,
    UpsertContactCommand,
)
from marketing_agents.application.ports.connectors import (
    AuthorizedConnectorCommand,
    ConnectorObservation,
    ConnectorWriteResult,
)
from marketing_agents.infrastructure.adapters.connectors.mock.base import (
    InMemoryMockReceiptLedger,
    MockReceiptLedger,
    build_read_observation,
    execute_mock_write,
)
from marketing_agents.infrastructure.adapters.connectors.registry import (
    ConnectorBundleConfigurationError,
    ConnectorModeSettings,
    ConnectorOperationRegistry,
    build_connector_registry,
)
from marketing_agents.infrastructure.catalog.models import CompiledCatalog


class MockSocialConnector:
    def __init__(self, registry: ConnectorOperationRegistry) -> None:
        self._registry = registry

    async def read_posts(self, request: ReadPostsRequest) -> ConnectorObservation[PostsPayload]:
        return build_read_observation(
            self._registry.declaration(request.capability_id),
            request,
            PostsPayload,
            request.parameters.resource_ids,
        )

    async def read_comments(
        self, request: ReadCommentsRequest
    ) -> ConnectorObservation[CommentsPayload]:
        return build_read_observation(
            self._registry.declaration(request.capability_id),
            request,
            CommentsPayload,
            request.parameters.resource_ids,
        )

    async def read_metrics(
        self, request: ReadMetricsRequest
    ) -> ConnectorObservation[MetricsPayload]:
        return build_read_observation(
            self._registry.declaration(request.capability_id),
            request,
            MetricsPayload,
            request.parameters.resource_ids,
        )

    async def read_profiles(
        self, request: ReadProfilesRequest
    ) -> ConnectorObservation[ProfilesPayload]:
        return build_read_observation(
            self._registry.declaration(request.capability_id),
            request,
            ProfilesPayload,
            request.parameters.resource_ids,
        )


class MockNewsletterConnector:
    def __init__(self, registry: ConnectorOperationRegistry, ledger: MockReceiptLedger) -> None:
        self._registry = registry
        self._ledger = ledger

    async def subscribe(
        self, request: AuthorizedConnectorCommand[SubscribeContactCommand]
    ) -> ConnectorWriteResult:
        return await execute_mock_write(
            self._registry.declaration("cap.newsletter.subscribe"), request, self._ledger
        )

    async def unsubscribe(
        self, request: AuthorizedConnectorCommand[UnsubscribeContactCommand]
    ) -> ConnectorWriteResult:
        return await execute_mock_write(
            self._registry.declaration("cap.newsletter.unsubscribe"), request, self._ledger
        )

    async def send_message(
        self, request: AuthorizedConnectorCommand[SendEmailCommand]
    ) -> ConnectorWriteResult:
        return await execute_mock_write(
            self._registry.declaration("cap.email.send-message"), request, self._ledger
        )


class MockCrmConnector:
    def __init__(self, registry: ConnectorOperationRegistry, ledger: MockReceiptLedger) -> None:
        self._registry = registry
        self._ledger = ledger

    async def read_customer(
        self, request: ReadCustomerRequest
    ) -> ConnectorObservation[CustomerPayload]:
        return build_read_observation(
            self._registry.declaration(request.capability_id),
            request,
            CustomerPayload,
            request.parameters.resource_ids,
        )

    async def upsert_contact(
        self, request: AuthorizedConnectorCommand[UpsertContactCommand]
    ) -> ConnectorWriteResult:
        return await execute_mock_write(
            self._registry.declaration("cap.crm.upsert-contact"), request, self._ledger
        )


class MockCmsConnector:
    def __init__(self, registry: ConnectorOperationRegistry) -> None:
        self._registry = registry

    async def read_content(
        self, request: ReadContentRequest
    ) -> ConnectorObservation[ContentMetadataPayload]:
        return build_read_observation(
            self._registry.declaration(request.capability_id),
            request,
            ContentMetadataPayload,
            request.parameters.resource_ids,
        )


class MockEventsConnector:
    def __init__(self, registry: ConnectorOperationRegistry, ledger: MockReceiptLedger) -> None:
        self._registry = registry
        self._ledger = ledger

    async def read_sessions(
        self, request: ReadSessionsRequest
    ) -> ConnectorObservation[SessionsPayload]:
        return build_read_observation(
            self._registry.declaration(request.capability_id),
            request,
            SessionsPayload,
            request.parameters.resource_ids,
        )

    async def read_attendance(
        self, request: ReadAttendanceRequest
    ) -> ConnectorObservation[AttendancePayload]:
        return build_read_observation(
            self._registry.declaration(request.capability_id),
            request,
            AttendancePayload,
            request.parameters.resource_ids,
        )

    async def enroll_attendee(
        self, request: AuthorizedConnectorCommand[EnrollAttendeeCommand]
    ) -> ConnectorWriteResult:
        return await execute_mock_write(
            self._registry.declaration("cap.events.enroll-attendee"), request, self._ledger
        )


class MockCommunityConnector:
    def __init__(self, registry: ConnectorOperationRegistry, ledger: MockReceiptLedger) -> None:
        self._registry = registry
        self._ledger = ledger

    async def read_membership(
        self, request: ReadMembershipRequest
    ) -> ConnectorObservation[MembershipPayload]:
        return build_read_observation(
            self._registry.declaration(request.capability_id),
            request,
            MembershipPayload,
            request.parameters.resource_ids,
        )

    async def read_course_progress(
        self, request: ReadCourseProgressRequest
    ) -> ConnectorObservation[CourseProgressPayload]:
        return build_read_observation(
            self._registry.declaration(request.capability_id),
            request,
            CourseProgressPayload,
            request.parameters.resource_ids,
        )

    async def send_message(
        self, request: AuthorizedConnectorCommand[SendCommunityMessageCommand]
    ) -> ConnectorWriteResult:
        return await execute_mock_write(
            self._registry.declaration("cap.messaging.send-message"), request, self._ledger
        )

    async def share_material(
        self, request: AuthorizedConnectorCommand[ShareMaterialCommand]
    ) -> ConnectorWriteResult:
        return await execute_mock_write(
            self._registry.declaration("cap.messaging.share-material"), request, self._ledger
        )


class MockSpreadsheetConnector:
    def __init__(self, registry: ConnectorOperationRegistry, ledger: MockReceiptLedger) -> None:
        self._registry = registry
        self._ledger = ledger

    async def read_range(
        self, request: ReadRangeRequest
    ) -> ConnectorObservation[SpreadsheetRangePayload]:
        resource_id = f"{request.parameters.document_ref}:{request.parameters.range_a1}"
        return build_read_observation(
            self._registry.declaration(request.capability_id),
            request,
            SpreadsheetRangePayload,
            (resource_id,),
        )

    async def update_rows(
        self, request: AuthorizedConnectorCommand[UpdateRowsCommand]
    ) -> ConnectorWriteResult:
        return await execute_mock_write(
            self._registry.declaration("cap.spreadsheet.update-rows"), request, self._ledger
        )


class MockFulfillmentConnector:
    def __init__(self, registry: ConnectorOperationRegistry) -> None:
        self._registry = registry

    async def read_status(
        self, request: ReadFulfillmentStatusRequest
    ) -> ConnectorObservation[FulfillmentStatusPayload]:
        return build_read_observation(
            self._registry.declaration(request.capability_id),
            request,
            FulfillmentStatusPayload,
            request.parameters.resource_ids,
        )


@dataclass(frozen=True, slots=True)
class MockConnectorBundle:
    registry: ConnectorOperationRegistry
    ledger: MockReceiptLedger
    social: MockSocialConnector
    newsletter: MockNewsletterConnector
    crm: MockCrmConnector
    cms: MockCmsConnector
    events: MockEventsConnector
    community: MockCommunityConnector
    spreadsheet: MockSpreadsheetConnector
    fulfillment: MockFulfillmentConnector

    @classmethod
    def create(
        cls,
        registry: ConnectorOperationRegistry,
        ledger: MockReceiptLedger | None = None,
    ) -> MockConnectorBundle:
        ledger = ledger or InMemoryMockReceiptLedger()
        return cls(
            registry=registry,
            ledger=ledger,
            social=MockSocialConnector(registry),
            newsletter=MockNewsletterConnector(registry, ledger),
            crm=MockCrmConnector(registry, ledger),
            cms=MockCmsConnector(registry),
            events=MockEventsConnector(registry, ledger),
            community=MockCommunityConnector(registry, ledger),
            spreadsheet=MockSpreadsheetConnector(registry, ledger),
            fulfillment=MockFulfillmentConnector(registry),
        )


def build_connector_bundle(
    settings: ConnectorModeSettings, catalog: CompiledCatalog
) -> MockConnectorBundle:
    """Compose only the configured mock bundle; real selections never fall back."""

    if settings.connector_mode != "mock":
        raise ConnectorBundleConfigurationError(
            f"connector mode {settings.connector_mode!r} is not explicitly registered"
        )
    if settings.allow_external_network or settings.real_connector_opt_in:
        raise ConnectorBundleConfigurationError(
            "mock connectors cannot be composed with external-network or real-mode opt-ins"
        )
    return MockConnectorBundle.create(build_connector_registry(catalog))

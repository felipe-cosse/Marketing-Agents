"""Capability-specific DTOs and async protocols for the eight connector families."""

from __future__ import annotations

from typing import Annotated, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from marketing_agents.application.ports.connectors import (
    AuthorizedConnectorCommand,
    ConnectorObservation,
    ConnectorReadRequest,
    ConnectorWriteResult,
)


class FrozenConnectorModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


ConnectorIdentifier = Annotated[str, Field(min_length=1, max_length=200)]
MetricName = Annotated[str, Field(min_length=1, max_length=120)]


class ExplicitIds(FrozenConnectorModel):
    resource_ids: tuple[ConnectorIdentifier, ...] = Field(min_length=1, max_length=100)


class MetricsParameters(ExplicitIds):
    metric_names: tuple[MetricName, ...] = Field(min_length=1, max_length=32)


class CourseProgressParameters(ExplicitIds):
    course_ref: str = Field(min_length=1, max_length=200)


class SpreadsheetRangeParameters(FrozenConnectorModel):
    document_ref: str = Field(min_length=1, max_length=200)
    range_a1: str = Field(min_length=1, max_length=100)
    max_rows: int = Field(default=100, ge=1, le=1_000)
    max_columns: int = Field(default=20, ge=1, le=100)


class MockConnectorRecord(FrozenConnectorModel):
    resource_id: str = Field(min_length=1, max_length=200)
    attributes: dict[str, JsonValue] = Field(max_length=32)


class RecordsPayload(FrozenConnectorModel):
    records: tuple[MockConnectorRecord, ...] = Field(max_length=1_000)


class PostsPayload(RecordsPayload):
    pass


class CommentsPayload(RecordsPayload):
    pass


class MetricsPayload(RecordsPayload):
    pass


class ProfilesPayload(RecordsPayload):
    pass


class CustomerPayload(RecordsPayload):
    pass


class ContentMetadataPayload(RecordsPayload):
    pass


class SessionsPayload(RecordsPayload):
    pass


class AttendancePayload(RecordsPayload):
    pass


class MembershipPayload(RecordsPayload):
    pass


class CourseProgressPayload(RecordsPayload):
    pass


class SpreadsheetRangePayload(RecordsPayload):
    pass


class FulfillmentStatusPayload(RecordsPayload):
    pass


class ReadPostsRequest(ConnectorReadRequest[ExplicitIds]):
    capability_id: Literal["cap.social.read-posts"] = "cap.social.read-posts"


class ReadCommentsRequest(ConnectorReadRequest[ExplicitIds]):
    capability_id: Literal["cap.social.read-comments"] = "cap.social.read-comments"


class ReadMetricsRequest(ConnectorReadRequest[MetricsParameters]):
    capability_id: Literal["cap.social.read-metrics"] = "cap.social.read-metrics"


class ReadProfilesRequest(ConnectorReadRequest[ExplicitIds]):
    capability_id: Literal["cap.social.read-profiles"] = "cap.social.read-profiles"


class ReadCustomerRequest(ConnectorReadRequest[ExplicitIds]):
    capability_id: Literal["cap.crm.read-customer"] = "cap.crm.read-customer"


class ReadContentRequest(ConnectorReadRequest[ExplicitIds]):
    capability_id: Literal["cap.cms.read-content"] = "cap.cms.read-content"


class ReadSessionsRequest(ConnectorReadRequest[ExplicitIds]):
    capability_id: Literal["cap.events.read-sessions"] = "cap.events.read-sessions"


class ReadAttendanceRequest(ConnectorReadRequest[ExplicitIds]):
    capability_id: Literal["cap.events.read-attendance"] = "cap.events.read-attendance"


class ReadMembershipRequest(ConnectorReadRequest[ExplicitIds]):
    capability_id: Literal["cap.community.read-membership"] = "cap.community.read-membership"


class ReadCourseProgressRequest(ConnectorReadRequest[CourseProgressParameters]):
    capability_id: Literal["cap.community.read-course-progress"] = (
        "cap.community.read-course-progress"
    )


class ReadRangeRequest(ConnectorReadRequest[SpreadsheetRangeParameters]):
    capability_id: Literal["cap.spreadsheet.read-range"] = "cap.spreadsheet.read-range"


class ReadFulfillmentStatusRequest(ConnectorReadRequest[ExplicitIds]):
    capability_id: Literal["cap.fulfillment.read-status"] = "cap.fulfillment.read-status"


class ContactListCommand(FrozenConnectorModel):
    contact_ref: str = Field(min_length=1, max_length=200)
    list_ref: str = Field(min_length=1, max_length=200)


class SubscribeContactCommand(ContactListCommand):
    pass


class UnsubscribeContactCommand(ContactListCommand):
    pass


class SendEmailCommand(FrozenConnectorModel):
    contact_ref: str = Field(min_length=1, max_length=200)
    subject: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=20_000)


class UpsertContactCommand(FrozenConnectorModel):
    contact_ref: str = Field(min_length=1, max_length=200)
    fields: dict[str, JsonValue] = Field(min_length=1, max_length=100)


class EnrollAttendeeCommand(FrozenConnectorModel):
    attendee_ref: str = Field(min_length=1, max_length=200)
    session_ref: str = Field(min_length=1, max_length=200)


class SendCommunityMessageCommand(FrozenConnectorModel):
    recipient_refs: tuple[str, ...] = Field(min_length=1, max_length=100)
    body: str = Field(min_length=1, max_length=20_000)


class ShareMaterialCommand(FrozenConnectorModel):
    recipient_refs: tuple[str, ...] = Field(min_length=1, max_length=100)
    artifact_id: str = Field(min_length=1, max_length=240)


class UpdateRowsCommand(FrozenConnectorModel):
    document_ref: str = Field(min_length=1, max_length=200)
    range_a1: str = Field(min_length=1, max_length=100)
    rows: tuple[dict[str, JsonValue], ...] = Field(min_length=1, max_length=100)


class ReservedCmsMutationCommand(FrozenConnectorModel):
    """Future DTO only: deliberately absent from every v1 protocol and registry."""

    content_ref: str = Field(min_length=1, max_length=200)
    artifact_id: str = Field(min_length=1, max_length=240)


class ReservedCreateFulfillmentCommand(FrozenConnectorModel):
    """Future DTO only: deliberately absent from every v1 protocol and registry."""

    order_ref: str = Field(min_length=1, max_length=200)
    item_refs: tuple[str, ...] = Field(min_length=1, max_length=100)


@runtime_checkable
class SocialConnector(Protocol):
    async def read_posts(self, request: ReadPostsRequest) -> ConnectorObservation[PostsPayload]: ...

    async def read_comments(
        self, request: ReadCommentsRequest
    ) -> ConnectorObservation[CommentsPayload]: ...

    async def read_metrics(
        self, request: ReadMetricsRequest
    ) -> ConnectorObservation[MetricsPayload]: ...

    async def read_profiles(
        self, request: ReadProfilesRequest
    ) -> ConnectorObservation[ProfilesPayload]: ...


@runtime_checkable
class NewsletterConnector(Protocol):
    async def subscribe(
        self, request: AuthorizedConnectorCommand[SubscribeContactCommand]
    ) -> ConnectorWriteResult: ...

    async def unsubscribe(
        self, request: AuthorizedConnectorCommand[UnsubscribeContactCommand]
    ) -> ConnectorWriteResult: ...

    async def send_message(
        self, request: AuthorizedConnectorCommand[SendEmailCommand]
    ) -> ConnectorWriteResult: ...


@runtime_checkable
class CrmConnector(Protocol):
    async def read_customer(
        self, request: ReadCustomerRequest
    ) -> ConnectorObservation[CustomerPayload]: ...

    async def upsert_contact(
        self, request: AuthorizedConnectorCommand[UpsertContactCommand]
    ) -> ConnectorWriteResult: ...


@runtime_checkable
class CmsConnector(Protocol):
    async def read_content(
        self, request: ReadContentRequest
    ) -> ConnectorObservation[ContentMetadataPayload]: ...


@runtime_checkable
class EventsConnector(Protocol):
    async def read_sessions(
        self, request: ReadSessionsRequest
    ) -> ConnectorObservation[SessionsPayload]: ...

    async def read_attendance(
        self, request: ReadAttendanceRequest
    ) -> ConnectorObservation[AttendancePayload]: ...

    async def enroll_attendee(
        self, request: AuthorizedConnectorCommand[EnrollAttendeeCommand]
    ) -> ConnectorWriteResult: ...


@runtime_checkable
class CommunityConnector(Protocol):
    async def read_membership(
        self, request: ReadMembershipRequest
    ) -> ConnectorObservation[MembershipPayload]: ...

    async def read_course_progress(
        self, request: ReadCourseProgressRequest
    ) -> ConnectorObservation[CourseProgressPayload]: ...

    async def send_message(
        self, request: AuthorizedConnectorCommand[SendCommunityMessageCommand]
    ) -> ConnectorWriteResult: ...

    async def share_material(
        self, request: AuthorizedConnectorCommand[ShareMaterialCommand]
    ) -> ConnectorWriteResult: ...


@runtime_checkable
class SpreadsheetConnector(Protocol):
    async def read_range(
        self, request: ReadRangeRequest
    ) -> ConnectorObservation[SpreadsheetRangePayload]: ...

    async def update_rows(
        self, request: AuthorizedConnectorCommand[UpdateRowsCommand]
    ) -> ConnectorWriteResult: ...


@runtime_checkable
class FulfillmentConnector(Protocol):
    async def read_status(
        self, request: ReadFulfillmentStatusRequest
    ) -> ConnectorObservation[FulfillmentStatusPayload]: ...

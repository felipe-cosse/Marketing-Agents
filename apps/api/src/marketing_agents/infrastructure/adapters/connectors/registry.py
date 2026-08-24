"""Immutable connector operation matrix cross-checked against the compiled catalog."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Protocol

from pydantic import BaseModel

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
from marketing_agents.application.ports.connectors import ConnectorWriteResult
from marketing_agents.domain.data_classification import DataClassification
from marketing_agents.domain.enums import Effect
from marketing_agents.infrastructure.catalog.models import CompiledCatalog

EXTERNAL_CONNECTOR_FAMILIES = frozenset(
    {
        "social",
        "newsletter",
        "crm",
        "cms",
        "events",
        "community",
        "spreadsheet",
        "fulfillment",
    }
)
NON_CONNECTOR_FAMILIES = frozenset({"model", "artifact"})
DISABLED_V1_CAPABILITIES = frozenset({"cap.email.send-message", "cap.spreadsheet.update-rows"})


class ConnectorBundleConfigurationError(ValueError):
    """Raised when adapter metadata or selected connector mode is not exact."""


class ConnectorModeSettings(Protocol):
    connector_mode: str
    allow_external_network: bool
    real_connector_opt_in: bool


@dataclass(frozen=True, slots=True)
class ConnectorOperationMetadata:
    capability_id: str
    connector_family: str
    effect: Effect
    request_schema_id: str
    result_schema_id: str
    idempotency_support: Literal["not_applicable", "required", "supported", "unavailable"]
    default_timeout_seconds: int
    data_classification: DataClassification
    rate_limit_scope: str
    request_redaction_fields: tuple[str, ...]
    result_redaction_fields: tuple[str, ...]
    enabled: bool = True
    disabled_reason: str | None = None

    def __post_init__(self) -> None:
        values = (
            self.capability_id,
            self.connector_family,
            self.request_schema_id,
            self.result_schema_id,
            self.rate_limit_scope,
        )
        if any(not value or value != value.strip() for value in values):
            raise ConnectorBundleConfigurationError("connector metadata must be normalized")
        if self.connector_family not in EXTERNAL_CONNECTOR_FAMILIES:
            raise ConnectorBundleConfigurationError("unsupported external connector family")
        if not 1 <= self.default_timeout_seconds <= 120:
            raise ConnectorBundleConfigurationError("connector timeout must be 1..120 seconds")
        if self.effect is Effect.WRITE and self.idempotency_support != "required":
            raise ConnectorBundleConfigurationError(
                "v1 connector writes must require provider idempotency"
            )
        if self.enabled == (self.disabled_reason is not None):
            raise ConnectorBundleConfigurationError(
                "disabled connector operations require one reason and enabled operations none"
            )


@dataclass(frozen=True, slots=True)
class ConnectorOperationRegistration:
    metadata: ConnectorOperationMetadata
    request_type: type[BaseModel]
    result_type: type[BaseModel] | type[ConnectorWriteResult]
    method_name: str

    def __post_init__(self) -> None:
        if not self.method_name or self.method_name != self.method_name.strip():
            raise ConnectorBundleConfigurationError("connector method name must be normalized")


def _schema_id(capability_id: str, direction: Literal["request", "result"]) -> str:
    return f"schema:connector:{capability_id.removeprefix('cap.')}:{direction}:v1"


def _registration(
    capability_id: str,
    family: str,
    effect: Effect,
    request_type: type[BaseModel],
    result_type: type[BaseModel] | type[ConnectorWriteResult],
    method_name: str,
    classification: DataClassification,
    *,
    request_redaction_fields: tuple[str, ...] = (),
    result_redaction_fields: tuple[str, ...] = (),
    enabled: bool = True,
) -> ConnectorOperationRegistration:
    return ConnectorOperationRegistration(
        metadata=ConnectorOperationMetadata(
            capability_id=capability_id,
            connector_family=family,
            effect=effect,
            request_schema_id=_schema_id(capability_id, "request"),
            result_schema_id=_schema_id(capability_id, "result"),
            idempotency_support="required" if effect is Effect.WRITE else "not_applicable",
            default_timeout_seconds=30,
            data_classification=classification,
            rate_limit_scope=f"connector:{family}:{capability_id.removeprefix('cap.')}",
            request_redaction_fields=request_redaction_fields,
            result_redaction_fields=result_redaction_fields,
            enabled=enabled,
            disabled_reason=None if enabled else "unassigned_in_v1",
        ),
        request_type=request_type,
        result_type=result_type,
        method_name=method_name,
    )


OPERATION_REGISTRATIONS = (
    _registration(
        "cap.social.read-posts",
        "social",
        Effect.READ,
        ReadPostsRequest,
        PostsPayload,
        "read_posts",
        DataClassification.INTERNAL,
    ),
    _registration(
        "cap.social.read-comments",
        "social",
        Effect.READ,
        ReadCommentsRequest,
        CommentsPayload,
        "read_comments",
        DataClassification.PERSONAL,
        result_redaction_fields=("/records/*/attributes/author_ref",),
    ),
    _registration(
        "cap.social.read-metrics",
        "social",
        Effect.READ,
        ReadMetricsRequest,
        MetricsPayload,
        "read_metrics",
        DataClassification.INTERNAL,
    ),
    _registration(
        "cap.social.read-profiles",
        "social",
        Effect.READ,
        ReadProfilesRequest,
        ProfilesPayload,
        "read_profiles",
        DataClassification.PUBLIC,
    ),
    _registration(
        "cap.newsletter.subscribe",
        "newsletter",
        Effect.WRITE,
        SubscribeContactCommand,
        ConnectorWriteResult,
        "subscribe",
        DataClassification.PERSONAL,
        request_redaction_fields=("/contact_ref",),
    ),
    _registration(
        "cap.newsletter.unsubscribe",
        "newsletter",
        Effect.WRITE,
        UnsubscribeContactCommand,
        ConnectorWriteResult,
        "unsubscribe",
        DataClassification.PERSONAL,
        request_redaction_fields=("/contact_ref",),
    ),
    _registration(
        "cap.email.send-message",
        "newsletter",
        Effect.WRITE,
        SendEmailCommand,
        ConnectorWriteResult,
        "send_message",
        DataClassification.PERSONAL,
        request_redaction_fields=("/contact_ref", "/subject", "/body"),
        enabled=False,
    ),
    _registration(
        "cap.crm.read-customer",
        "crm",
        Effect.READ,
        ReadCustomerRequest,
        CustomerPayload,
        "read_customer",
        DataClassification.PERSONAL,
        result_redaction_fields=("/records/*",),
    ),
    _registration(
        "cap.crm.upsert-contact",
        "crm",
        Effect.WRITE,
        UpsertContactCommand,
        ConnectorWriteResult,
        "upsert_contact",
        DataClassification.PERSONAL,
        request_redaction_fields=("/contact_ref", "/fields"),
    ),
    _registration(
        "cap.cms.read-content",
        "cms",
        Effect.READ,
        ReadContentRequest,
        ContentMetadataPayload,
        "read_content",
        DataClassification.INTERNAL,
    ),
    _registration(
        "cap.events.read-sessions",
        "events",
        Effect.READ,
        ReadSessionsRequest,
        SessionsPayload,
        "read_sessions",
        DataClassification.INTERNAL,
    ),
    _registration(
        "cap.events.read-attendance",
        "events",
        Effect.READ,
        ReadAttendanceRequest,
        AttendancePayload,
        "read_attendance",
        DataClassification.PERSONAL,
        result_redaction_fields=("/records/*",),
    ),
    _registration(
        "cap.events.enroll-attendee",
        "events",
        Effect.WRITE,
        EnrollAttendeeCommand,
        ConnectorWriteResult,
        "enroll_attendee",
        DataClassification.PERSONAL,
        request_redaction_fields=("/attendee_ref",),
    ),
    _registration(
        "cap.community.read-membership",
        "community",
        Effect.READ,
        ReadMembershipRequest,
        MembershipPayload,
        "read_membership",
        DataClassification.PERSONAL,
        result_redaction_fields=("/records/*",),
    ),
    _registration(
        "cap.community.read-course-progress",
        "community",
        Effect.READ,
        ReadCourseProgressRequest,
        CourseProgressPayload,
        "read_course_progress",
        DataClassification.PERSONAL,
        result_redaction_fields=("/records/*",),
    ),
    _registration(
        "cap.messaging.send-message",
        "community",
        Effect.WRITE,
        SendCommunityMessageCommand,
        ConnectorWriteResult,
        "send_message",
        DataClassification.PERSONAL,
        request_redaction_fields=("/recipient_refs", "/body"),
    ),
    _registration(
        "cap.messaging.share-material",
        "community",
        Effect.WRITE,
        ShareMaterialCommand,
        ConnectorWriteResult,
        "share_material",
        DataClassification.PERSONAL,
        request_redaction_fields=("/recipient_refs",),
    ),
    _registration(
        "cap.spreadsheet.read-range",
        "spreadsheet",
        Effect.READ,
        ReadRangeRequest,
        SpreadsheetRangePayload,
        "read_range",
        DataClassification.INTERNAL,
    ),
    _registration(
        "cap.spreadsheet.update-rows",
        "spreadsheet",
        Effect.WRITE,
        UpdateRowsCommand,
        ConnectorWriteResult,
        "update_rows",
        DataClassification.INTERNAL,
        enabled=False,
    ),
    _registration(
        "cap.fulfillment.read-status",
        "fulfillment",
        Effect.READ,
        ReadFulfillmentStatusRequest,
        FulfillmentStatusPayload,
        "read_status",
        DataClassification.PERSONAL,
        result_redaction_fields=("/records/*",),
    ),
)


class ConnectorOperationRegistry:
    """Exact immutable capability-to-operation mapping."""

    __slots__ = ("_operations",)

    def __init__(self, registrations: Iterable[ConnectorOperationRegistration]) -> None:
        operations: dict[str, ConnectorOperationRegistration] = {}
        for registration in registrations:
            capability_id = registration.metadata.capability_id
            if capability_id in operations:
                raise ConnectorBundleConfigurationError(
                    f"duplicate connector capability {capability_id!r}"
                )
            operations[capability_id] = registration
        self._operations = MappingProxyType(operations)

    @property
    def operations(self) -> tuple[ConnectorOperationRegistration, ...]:
        return tuple(self._operations[key] for key in sorted(self._operations))

    @property
    def capability_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._operations))

    def declaration(self, capability_id: str) -> ConnectorOperationRegistration:
        try:
            return self._operations[capability_id]
        except KeyError as exc:
            raise ConnectorBundleConfigurationError(
                f"connector capability {capability_id!r} is not registered"
            ) from exc

    def resolve(self, capability_id: str) -> ConnectorOperationRegistration:
        registration = self.declaration(capability_id)
        if not registration.metadata.enabled:
            raise ConnectorBundleConfigurationError(
                f"connector capability {capability_id!r} is disabled: "
                f"{registration.metadata.disabled_reason}"
            )
        return registration

    def validate_catalog(self, catalog: CompiledCatalog) -> None:
        catalog_families = {item.connector_family for item in catalog.tool_capabilities}
        expected_families = EXTERNAL_CONNECTOR_FAMILIES | NON_CONNECTOR_FAMILIES
        if catalog_families != expected_families:
            raise ConnectorBundleConfigurationError(
                "catalog connector family set does not match the v1 registry boundary"
            )

        catalog_external = {
            item.id: item
            for item in catalog.tool_capabilities
            if item.connector_family in EXTERNAL_CONNECTOR_FAMILIES
        }
        if set(catalog_external) != set(self._operations):
            raise ConnectorBundleConfigurationError(
                "catalog external capability set does not match registered operations"
            )

        assigned = {
            capability_id
            for template in catalog.templates
            for capability_id in template.allowed_tool_capability_ids
        }
        disabled = {
            item.metadata.capability_id for item in self.operations if not item.metadata.enabled
        }
        if disabled != DISABLED_V1_CAPABILITIES or disabled & assigned:
            raise ConnectorBundleConfigurationError(
                "disabled connector operations must be the exact unassigned v1 write set"
            )

        for capability_id, catalog_item in catalog_external.items():
            metadata = self._operations[capability_id].metadata
            if metadata.request_schema_id != _schema_id(
                capability_id, "request"
            ) or metadata.result_schema_id != _schema_id(capability_id, "result"):
                raise ConnectorBundleConfigurationError(
                    f"connector schema identity drift for {capability_id!r}"
                )
            actual = (
                metadata.connector_family,
                metadata.effect.value,
                metadata.idempotency_support,
                metadata.default_timeout_seconds,
                metadata.data_classification.value,
            )
            expected = (
                catalog_item.connector_family,
                catalog_item.effect,
                catalog_item.idempotency_support,
                catalog_item.default_timeout_seconds,
                catalog_item.data_classification,
            )
            if actual != expected:
                raise ConnectorBundleConfigurationError(
                    f"connector metadata drift for {capability_id!r}"
                )
            if capability_id in assigned and not metadata.enabled:
                raise ConnectorBundleConfigurationError(
                    f"assigned capability {capability_id!r} cannot be disabled"
                )


def build_connector_registry(catalog: CompiledCatalog) -> ConnectorOperationRegistry:
    registry = ConnectorOperationRegistry(OPERATION_REGISTRATIONS)
    registry.validate_catalog(catalog)
    return registry

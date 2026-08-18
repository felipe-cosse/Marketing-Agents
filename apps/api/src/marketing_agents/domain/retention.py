"""Independent retention TTLs and safe expiry calculations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from marketing_agents.domain.data_classification import DataClassification


class RetentionCategory(StrEnum):
    ADMITTED_PAYLOAD = "admitted_payload"
    EXTERNAL_ACTION_PAYLOAD = "external_action_payload"
    APPROVAL_DETAIL = "approval_detail"
    ARTIFACT_DETAIL = "artifact_detail"
    CONNECTOR_RECEIPT_DETAIL = "connector_receipt_detail"
    AUDIT_METADATA = "audit_metadata"


@dataclass(frozen=True)
class RetentionPolicy:
    admitted_payload_days: int = 7
    external_action_payload_days: int = 7
    approval_detail_days: int = 7
    artifact_detail_days: int = 30
    connector_receipt_detail_days: int = 30
    audit_metadata_days: int = 90

    def __post_init__(self) -> None:
        for field_name, value in self.__dict__.items():
            if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 3_650:
                raise ValueError(f"{field_name} must be an integer from 1 through 3650")

    def ttl_for(self, category: RetentionCategory) -> timedelta:
        days = {
            RetentionCategory.ADMITTED_PAYLOAD: self.admitted_payload_days,
            RetentionCategory.EXTERNAL_ACTION_PAYLOAD: self.external_action_payload_days,
            RetentionCategory.APPROVAL_DETAIL: self.approval_detail_days,
            RetentionCategory.ARTIFACT_DETAIL: self.artifact_detail_days,
            RetentionCategory.CONNECTOR_RECEIPT_DETAIL: self.connector_receipt_detail_days,
            RetentionCategory.AUDIT_METADATA: self.audit_metadata_days,
        }[category]
        return timedelta(days=days)

    def expires_at(
        self,
        category: RetentionCategory,
        created_at: datetime,
        classification: DataClassification,
    ) -> datetime:
        if classification is DataClassification.SECRET:
            raise ValueError("secrets are never retainable")
        offset = created_at.utcoffset()
        if offset is None or offset.total_seconds() != 0:
            raise ValueError("retention timestamps must be UTC")
        return created_at.astimezone(UTC) + self.ttl_for(category)

    def is_expired(
        self,
        category: RetentionCategory,
        created_at: datetime,
        classification: DataClassification,
        now: datetime,
    ) -> bool:
        offset = now.utcoffset()
        if offset is None or offset.total_seconds() != 0:
            raise ValueError("retention clock must be UTC")
        return now >= self.expires_at(category, created_at, classification)

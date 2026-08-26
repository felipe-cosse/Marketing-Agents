"""Immutable, keyed-digest webhook receipt aggregates."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from marketing_agents.domain.validation import require_digest, require_id, require_utc

WEBHOOK_DIGEST_KEY_VERSION_PREFIX = "webhook-body-hmac-sha256-v1:"
WEBHOOK_DIGEST_KEY_VERSION_PATTERN = re.compile(
    rf"^{re.escape(WEBHOOK_DIGEST_KEY_VERSION_PREFIX)}[0-9a-f]{{64}}$"
)
MAX_WEBHOOK_RECEIPT_DELIVERIES = 43


@dataclass(frozen=True, slots=True)
class WebhookReceiptDelivery:
    """One immutable WorkItem/Run outcome for an explicitly bound instance."""

    instance_id: str
    work_item_id: str
    run_id: str

    def __post_init__(self) -> None:
        require_id(self.instance_id, "webhook receipt delivery instance ID")
        require_id(self.work_item_id, "webhook receipt delivery WorkItem ID")
        require_id(self.run_id, "webhook receipt delivery Run ID")


@dataclass(frozen=True, slots=True, repr=False)
class WebhookReceipt:
    """Authenticated source-event receipt with its complete fan-out outcome."""

    id: str
    source: str
    event_id: str
    trigger_id: str
    body_digest: str
    digest_key_version: str
    mapper_version: str
    received_at: datetime
    deliveries: tuple[WebhookReceiptDelivery, ...]

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.id, "webhook receipt ID"),
            (self.source, "webhook receipt source"),
            (self.event_id, "webhook receipt event ID"),
            (self.trigger_id, "webhook receipt trigger ID"),
            (self.mapper_version, "webhook receipt mapper version"),
        ):
            require_id(value, field_name)
        require_digest(self.body_digest, "webhook receipt body digest")
        if (
            type(self.digest_key_version) is not str
            or WEBHOOK_DIGEST_KEY_VERSION_PATTERN.fullmatch(self.digest_key_version) is None
        ):
            raise ValueError("webhook receipt digest key version is invalid")
        require_utc(self.received_at, "webhook receipt received time")
        if (
            type(self.deliveries) is not tuple
            or not 1 <= len(self.deliveries) <= MAX_WEBHOOK_RECEIPT_DELIVERIES
            or any(type(item) is not WebhookReceiptDelivery for item in self.deliveries)
        ):
            raise ValueError("webhook receipt deliveries must be one bounded immutable tuple")
        instance_ids = tuple(item.instance_id for item in self.deliveries)
        work_item_ids = tuple(item.work_item_id for item in self.deliveries)
        run_ids = tuple(item.run_id for item in self.deliveries)
        if (
            len(instance_ids) != len(set(instance_ids))
            or len(work_item_ids) != len(set(work_item_ids))
            or len(run_ids) != len(set(run_ids))
        ):
            raise ValueError("webhook receipt delivery identities must be unique")
        object.__setattr__(
            self,
            "deliveries",
            tuple(sorted(self.deliveries, key=lambda item: item.instance_id)),
        )

    def __repr__(self) -> str:
        return (
            "WebhookReceipt("
            f"id={self.id!r}, source={self.source!r}, event_id={self.event_id!r}, "
            f"trigger_id={self.trigger_id!r}, mapper_version={self.mapper_version!r}, "
            f"received_at={self.received_at!r}, deliveries={self.deliveries!r}, "
            "body_digest=[REDACTED], digest_key_version=[REDACTED])"
        )


__all__ = [
    "MAX_WEBHOOK_RECEIPT_DELIVERIES",
    "WEBHOOK_DIGEST_KEY_VERSION_PATTERN",
    "WEBHOOK_DIGEST_KEY_VERSION_PREFIX",
    "WebhookReceipt",
    "WebhookReceiptDelivery",
]

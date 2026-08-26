"""Typed transport projections for authenticated webhook admission."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from marketing_agents.domain.webhook import MAX_WEBHOOK_RECEIPT_DELIVERIES


class WebhookApiModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )


class WebhookDeliveryResponse(WebhookApiModel):
    instance_id: str
    work_id: str
    run_id: str
    instance_url: str
    run_url: str


class WebhookAdmissionResponse(WebhookApiModel):
    status: Literal["accepted"]
    disposition: Literal["created", "replayed"]
    source: str
    event_id: str
    receipt_id: str
    deliveries: tuple[WebhookDeliveryResponse, ...] = Field(
        min_length=1,
        max_length=MAX_WEBHOOK_RECEIPT_DELIVERIES,
    )


class WebhookProblem(WebhookApiModel):
    code: str
    message: str
    pointer: str | None = Field(
        default=None,
        max_length=1_000,
        pattern=r"^/input(?:/[A-Za-z0-9_.-]{1,100}){0,64}$",
    )


class WebhookHttpError(WebhookApiModel):
    detail: str


__all__ = [
    "WebhookAdmissionResponse",
    "WebhookDeliveryResponse",
    "WebhookHttpError",
    "WebhookProblem",
]

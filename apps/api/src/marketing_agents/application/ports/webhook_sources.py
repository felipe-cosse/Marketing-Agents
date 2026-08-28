"""Registered, server-owned webhook verifier and input-mapper definitions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from marketing_agents.application.ports.webhooks import (
    WebhookSignatureVerifier,
    WebhookVerifierConfig,
    require_webhook_source_id,
    require_webhook_trigger_id,
)
from marketing_agents.domain.validation import frozen_json_mapping, require_id

DEFAULT_WEBHOOK_ADMISSION_RATE_MAX_CALLS = 60
DEFAULT_WEBHOOK_ADMISSION_RATE_WINDOW_SECONDS = 60
MAX_WEBHOOK_ADMISSION_RATE_MAX_CALLS = 10_000
MAX_WEBHOOK_ADMISSION_RATE_WINDOW_SECONDS = 3_600


class WebhookEnvelopeMappingError(ValueError):
    def __init__(self, code: str, message: str, *, pointer: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.pointer = pointer


@dataclass(frozen=True, slots=True, kw_only=True)
class MappedWebhookEnvelope:
    event_id: str
    input_payload: Mapping[str, Any] = field(repr=False)

    def __post_init__(self) -> None:
        require_id(self.event_id, "mapped webhook event ID")
        object.__setattr__(
            self,
            "input_payload",
            frozen_json_mapping(self.input_payload, "mapped webhook input payload"),
        )


class WebhookEnvelopeMapper(Protocol):
    version: str

    def parse(self, raw_body: bytes) -> MappedWebhookEnvelope: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class WebhookSourceDefinition:
    source: str
    trigger_id: str
    mapper_version: str
    signature_verifier: WebhookSignatureVerifier = field(repr=False)
    verifier_config: WebhookVerifierConfig = field(repr=False)
    mapper: WebhookEnvelopeMapper = field(repr=False)
    admission_rate_max_calls: int = DEFAULT_WEBHOOK_ADMISSION_RATE_MAX_CALLS
    admission_rate_window_seconds: int = DEFAULT_WEBHOOK_ADMISSION_RATE_WINDOW_SECONDS

    def __post_init__(self) -> None:
        require_webhook_source_id(self.source, "webhook source definition source")
        require_webhook_trigger_id(self.trigger_id, "webhook source definition trigger ID")
        require_id(self.mapper_version, "webhook source definition mapper version")
        if not callable(getattr(self.signature_verifier, "verify", None)):
            raise ValueError("webhook source definition requires a signature verifier")
        if type(self.verifier_config) is not WebhookVerifierConfig:
            raise ValueError("webhook source definition requires exact verifier configuration")
        if not callable(getattr(self.mapper, "parse", None)):
            raise ValueError("webhook source definition requires an envelope mapper")
        if getattr(self.mapper, "version", None) != self.mapper_version:
            raise ValueError("webhook mapper version must match its immutable implementation")
        if (
            type(self.admission_rate_max_calls) is not int
            or not 1 <= self.admission_rate_max_calls <= MAX_WEBHOOK_ADMISSION_RATE_MAX_CALLS
        ):
            raise ValueError("webhook admission rate call bound is invalid")
        if (
            type(self.admission_rate_window_seconds) is not int
            or not 1
            <= self.admission_rate_window_seconds
            <= MAX_WEBHOOK_ADMISSION_RATE_WINDOW_SECONDS
        ):
            raise ValueError("webhook admission rate window is invalid")


class WebhookSourceRegistry(Protocol):
    def resolve(self, source: str, trigger_id: str) -> WebhookSourceDefinition | None: ...


__all__ = [
    "DEFAULT_WEBHOOK_ADMISSION_RATE_MAX_CALLS",
    "DEFAULT_WEBHOOK_ADMISSION_RATE_WINDOW_SECONDS",
    "MAX_WEBHOOK_ADMISSION_RATE_MAX_CALLS",
    "MAX_WEBHOOK_ADMISSION_RATE_WINDOW_SECONDS",
    "MappedWebhookEnvelope",
    "WebhookEnvelopeMapper",
    "WebhookEnvelopeMappingError",
    "WebhookSourceDefinition",
    "WebhookSourceRegistry",
]

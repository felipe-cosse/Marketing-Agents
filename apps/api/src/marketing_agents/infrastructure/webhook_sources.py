"""Static registered webhook sources and strict deterministic envelope mapping."""

from __future__ import annotations

import json
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from marketing_agents.application.ports.webhook_sources import (
    MappedWebhookEnvelope,
    WebhookEnvelopeMappingError,
    WebhookSourceDefinition,
)
from marketing_agents.application.ports.webhooks import (
    require_webhook_source_id,
    require_webhook_trigger_id,
)
from marketing_agents.domain.validation import require_id

MAX_WEBHOOK_BODY_BYTES = 1_048_576
MAX_WEBHOOK_JSON_DEPTH = 64


def _mapping_error() -> WebhookEnvelopeMappingError:
    return WebhookEnvelopeMappingError(
        "webhook_envelope_invalid",
        "webhook envelope is invalid",
    )


def _reject_constant(_value: str) -> None:
    raise _mapping_error()


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _mapping_error()
        result[key] = value
    return result


def _depth_is_bounded(raw_body: bytes) -> bool:
    depth = 0
    in_string = False
    escaped = False
    for byte in raw_body:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                in_string = False
        elif byte == 0x22:
            in_string = True
        elif byte in {0x5B, 0x7B}:
            depth += 1
            if depth > MAX_WEBHOOK_JSON_DEPTH + 1:
                return False
        elif byte in {0x5D, 0x7D}:
            depth -= 1
            if depth < 0:
                return False
    return depth == 0 and not in_string


class StrictJsonWebhookEnvelopeMapper:
    """Accept only `{eventId,input}` after signature authentication."""

    version = "webhook-envelope-input-v1"

    def parse(self, raw_body: bytes) -> MappedWebhookEnvelope:
        if (
            type(raw_body) is not bytes
            or not raw_body
            or len(raw_body) > MAX_WEBHOOK_BODY_BYTES
            or raw_body.startswith(b"\xef\xbb\xbf")
            or not _depth_is_bounded(raw_body)
        ):
            raise _mapping_error()
        try:
            decoded = raw_body.decode("utf-8", errors="strict")
            value = json.loads(
                decoded,
                object_pairs_hook=_strict_object,
                parse_constant=_reject_constant,
            )
        except WebhookEnvelopeMappingError:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
            raise _mapping_error() from None
        if type(value) is not dict or set(value) != {"eventId", "input"}:
            raise _mapping_error()
        event_id = value["eventId"]
        input_payload = value["input"]
        try:
            require_id(event_id, "webhook event ID")
        except (TypeError, ValueError):
            raise _mapping_error() from None
        if type(input_payload) is not dict:
            raise WebhookEnvelopeMappingError(
                "webhook_envelope_invalid",
                "webhook envelope is invalid",
                pointer="/input",
            )
        try:
            return MappedWebhookEnvelope(event_id=event_id, input_payload=input_payload)
        except (TypeError, ValueError, RecursionError):
            raise _mapping_error() from None


class StaticWebhookSourceRegistry:
    """Fail closed unless a complete source/trigger hook was installed at composition."""

    def __init__(self, definitions: tuple[WebhookSourceDefinition, ...]) -> None:
        if type(definitions) is not tuple or any(
            type(item) is not WebhookSourceDefinition for item in definitions
        ):
            raise ValueError("webhook source definitions must be one exact tuple")
        indexed: dict[tuple[str, str], WebhookSourceDefinition] = {}
        sources: set[str] = set()
        secret_references: set[str] = set()
        verifier_ids: set[int] = set()
        for item in definitions:
            key = (item.source, item.trigger_id)
            if key in indexed:
                raise ValueError("webhook source definitions must be unique")
            if item.source in sources:
                raise ValueError("one webhook source may authorize only one registered trigger")
            secret_reference = item.verifier_config.secret_reference
            if secret_reference in secret_references:
                raise ValueError("webhook secret references must be unique per source authority")
            verifier_id = id(item.signature_verifier)
            if verifier_id in verifier_ids:
                raise ValueError("webhook verifier instances must be unique per source authority")
            indexed[key] = item
            sources.add(item.source)
            secret_references.add(secret_reference)
            verifier_ids.add(verifier_id)
        self._definitions: Mapping[tuple[str, str], WebhookSourceDefinition] = MappingProxyType(
            indexed
        )

    def resolve(self, source: str, trigger_id: str) -> WebhookSourceDefinition | None:
        try:
            require_webhook_source_id(source, "webhook source")
            require_webhook_trigger_id(trigger_id, "webhook trigger ID")
        except (TypeError, ValueError):
            return None
        return self._definitions.get((source, trigger_id))


__all__ = [
    "MAX_WEBHOOK_BODY_BYTES",
    "MAX_WEBHOOK_JSON_DEPTH",
    "StaticWebhookSourceRegistry",
    "StrictJsonWebhookEnvelopeMapper",
]

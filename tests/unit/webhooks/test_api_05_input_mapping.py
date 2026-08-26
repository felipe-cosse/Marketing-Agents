"""API-05: authenticated webhook bytes map through one strict allowlist."""

from __future__ import annotations

import pytest
from marketing_agents.application.ports.webhook_sources import (
    WebhookEnvelopeMappingError,
    WebhookSourceDefinition,
)
from marketing_agents.application.ports.webhooks import WebhookVerifierConfig
from marketing_agents.infrastructure.webhook_sources import (
    StaticWebhookSourceRegistry,
    StrictJsonWebhookEnvelopeMapper,
)


class _Verifier:
    def verify(self, **_kwargs: object) -> object:
        return object()


def _definition(
    *,
    source: str = "source.api05",
    trigger_id: str = "trigger.api05",
    secret_reference: str = "env:API05_SECRET_ONE",
    verifier: object | None = None,
    mapper_version: str = StrictJsonWebhookEnvelopeMapper.version,
) -> WebhookSourceDefinition:
    return WebhookSourceDefinition(
        source=source,
        trigger_id=trigger_id,
        mapper_version=mapper_version,
        signature_verifier=verifier or _Verifier(),  # type: ignore[arg-type]
        verifier_config=WebhookVerifierConfig(secret_reference=secret_reference),
        mapper=StrictJsonWebhookEnvelopeMapper(),
    )


def _mapper() -> StrictJsonWebhookEnvelopeMapper:
    return StrictJsonWebhookEnvelopeMapper()


def test_api_05_mapper_accepts_only_event_id_and_input_without_routing_authority() -> None:
    mapped = _mapper().parse(b'{"eventId":"event.api05.0001","input":{"topic":"safe","count":2}}')

    assert mapped.event_id == "event.api05.0001"
    assert mapped.input_payload == {"topic": "safe", "count": 2}
    assert "safe" not in repr(mapped)

    with pytest.raises(WebhookEnvelopeMappingError):
        _mapper().parse(b'{"eventId":"event.api05.0001","input":{},"instanceId":"inst.forged"}')


@pytest.mark.parametrize(
    "raw_body",
    (
        b'{"eventId":"event.one","eventId":"event.two","input":{}}',
        b'{"eventId":"event.one","input":{"value":NaN}}',
        b'\xef\xbb\xbf{"eventId":"event.one","input":{}}',
        b'{"eventId":"event.one","input":[]}',
        b'{"event_id":"event.one","input":{}}',
        b"[]",
        b"",
        b'{"eventId":"bad event","input":{}}',
    ),
)
def test_api_05_mapper_rejects_ambiguous_or_noncanonical_envelopes(raw_body: bytes) -> None:
    with pytest.raises(WebhookEnvelopeMappingError) as rejected:
        _mapper().parse(raw_body)

    assert rejected.value.code == "webhook_envelope_invalid"


def test_api_05_mapper_rejects_depth_before_recursive_json_normalization() -> None:
    raw_body = (
        b'{"eventId":"event.deep","input":' + (b'{"nested":' * 65) + b'"leaf"' + (b"}" * 65) + b"}"
    )

    with pytest.raises(WebhookEnvelopeMappingError):
        _mapper().parse(raw_body)


def test_api_05_source_definition_binds_the_mapper_implementation_version() -> None:
    with pytest.raises(ValueError, match="mapper version"):
        _definition(mapper_version="webhook-envelope-input-v999")


@pytest.mark.parametrize(
    "definitions",
    (
        (
            _definition(),
            _definition(
                trigger_id="trigger.api05.other",
                secret_reference="env:API05_SECRET_TWO",
            ),
        ),
        (
            _definition(),
            _definition(
                source="source.api05.other",
                trigger_id="trigger.api05.other",
            ),
        ),
    ),
)
def test_api_05_registry_rejects_shared_source_or_secret_authority(
    definitions: tuple[WebhookSourceDefinition, WebhookSourceDefinition],
) -> None:
    with pytest.raises(ValueError):
        StaticWebhookSourceRegistry(definitions)


def test_api_05_registry_rejects_reusing_one_verifier_authority() -> None:
    verifier = _Verifier()
    with pytest.raises(ValueError, match="verifier instances"):
        StaticWebhookSourceRegistry(
            (
                _definition(verifier=verifier),
                _definition(
                    source="source.api05.other",
                    trigger_id="trigger.api05.other",
                    secret_reference="env:API05_SECRET_TWO",
                    verifier=verifier,
                ),
            )
        )

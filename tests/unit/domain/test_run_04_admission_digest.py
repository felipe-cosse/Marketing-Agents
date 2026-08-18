"""RUN-04: canonical keyed admission identity is stable and complete."""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest
from marketing_agents.domain.admission import AdmissionEnvelope
from marketing_agents.domain.canonical_json import CanonicalJsonError, canonical_json_bytes
from marketing_agents.domain.enums import WorkMode
from marketing_agents.security.admission_digest import derive_admission_digests
from marketing_agents.security.digest_key import DigestKey


def _envelope() -> AdmissionEnvelope:
    return AdmissionEnvelope(
        source="manual",
        event_id="event.0001",
        instance_id="instance.email.01",
        trigger_id="trigger.manual.01",
        workflow_id="workflow.email-signup.v1",
        mode=WorkMode.MOCK_EXECUTION,
        brief_id="brief.signup.01",
        brief_revision=2,
        configuration_revision=3,
        admitted_payload={
            "profile": {"display_name": "Jose\u0301", "tags": ["new", "local"]},
            "email": "person@example.test",
        },
    )


def _key(offset: int = 0) -> DigestKey:
    return DigestKey(bytes((value + offset) % 256 for value in range(32)))


def test_run_04_digest_is_restart_stable_canonical_keyed_and_secret_safe() -> None:
    first = derive_admission_digests(_envelope(), _key())
    canonical_equivalent = replace(
        _envelope(),
        admitted_payload={
            "email": "person@example.test",
            "profile": {"tags": ["new", "local"], "display_name": "Jos\u00e9"},
        },
    )
    restarted = derive_admission_digests(canonical_equivalent, _key())

    assert restarted == first
    assert first.input_digest != first.admission_digest
    assert (
        first.input_digest
        != hashlib.sha256(canonical_json_bytes(_envelope().admitted_payload)).hexdigest()
    )
    assert repr(first) == "AdmissionDigests([REDACTED])"
    assert _key().bytes_for_digest().hex() not in repr(first)


def test_run_04_every_routing_context_or_payload_change_changes_admission_digest() -> None:
    original = _envelope()
    original_digests = derive_admission_digests(original, _key())
    mutations = (
        replace(original, source="webhook"),
        replace(original, event_id="event.0002"),
        replace(original, instance_id="instance.email.02"),
        replace(original, trigger_id="trigger.webhook.01"),
        replace(original, workflow_id="workflow.email-review.v1"),
        replace(original, mode=WorkMode.DRY_RUN),
        replace(original, brief_id="brief.signup.02"),
        replace(original, brief_revision=4),
        replace(original, configuration_revision=5),
        replace(original, admitted_payload={"email": "changed@example.test"}),
    )

    changed = [derive_admission_digests(item, _key()) for item in mutations]
    assert all(item.admission_digest != original_digests.admission_digest for item in changed)
    assert all(item.input_digest == original_digests.input_digest for item in changed[:-1])
    assert changed[-1].input_digest != original_digests.input_digest


def test_run_04_different_install_key_changes_version_and_both_digests() -> None:
    first = derive_admission_digests(_envelope(), _key())
    second = derive_admission_digests(_envelope(), _key(1))

    assert second.digest_key_version != first.digest_key_version
    assert second.input_digest != first.input_digest
    assert second.admission_digest != first.admission_digest


@pytest.mark.parametrize(
    "changes",
    [
        {"brief_id": None},
        {"brief_revision": None},
        {"brief_revision": 0},
        {"configuration_revision": 0},
        {"admitted_payload": {"invalid": float("nan")}},
        {"admitted_payload": {1: "non-string"}},
        {"admitted_payload": {"e\u0301": 1, "\u00e9": 2}},
    ],
)
def test_run_04_invalid_or_ambiguous_admission_context_is_rejected(
    changes: dict[str, object],
) -> None:
    with pytest.raises((ValueError, CanonicalJsonError)):
        replace(_envelope(), **changes)  # type: ignore[arg-type]


def test_run_04_admitted_payload_snapshot_is_recursively_immutable() -> None:
    envelope = _envelope()

    assert "person@example.test" not in repr(envelope)
    with pytest.raises(TypeError):
        envelope.admitted_payload["new"] = "value"  # type: ignore[index]
    profile = envelope.admitted_payload["profile"]
    assert isinstance(profile, dict) is False
    with pytest.raises(TypeError):
        profile["display_name"] = "changed"

"""API-05: restricted keyed webhook body comparison material."""

from __future__ import annotations

from marketing_agents.security.digest_key import DigestKey
from marketing_agents.security.webhook_digest import derive_webhook_body_digest


def test_api_05_body_digest_is_restart_stable_exact_byte_sensitive_and_redacted() -> None:
    key = DigestKey(bytes(range(32)))
    raw = b'{"eventId":"event.api05","input":{}}'

    first = derive_webhook_body_digest(raw, key)
    restarted = derive_webhook_body_digest(raw, DigestKey(bytes(range(32))))
    changed_bytes = derive_webhook_body_digest(raw + b" ", key)
    changed_key = derive_webhook_body_digest(raw, DigestKey(bytes(reversed(range(32)))))

    assert first.matches(restarted)
    assert not first.matches(changed_bytes)
    assert not first.matches(changed_key)
    assert first.value not in repr(first)
    assert first.digest_key_version not in repr(first)
    assert "REDACTED" in repr(first)

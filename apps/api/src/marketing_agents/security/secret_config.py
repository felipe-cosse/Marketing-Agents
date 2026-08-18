"""Backward-compatible exports for the central redaction implementation."""

from marketing_agents.security.redaction import (
    REDACTED,
    SENSITIVE_KEY_FRAGMENTS,
    SecretValue,
    is_sensitive_key,
    redact_config,
)

__all__ = [
    "REDACTED",
    "SENSITIVE_KEY_FRAGMENTS",
    "SecretValue",
    "is_sensitive_key",
    "redact_config",
]

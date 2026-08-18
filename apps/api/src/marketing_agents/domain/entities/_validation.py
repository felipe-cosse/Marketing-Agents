"""Compatibility re-exports for the package-independent domain validators."""

from marketing_agents.domain.validation import (
    DIGEST_PATTERN,
    ID_PATTERN,
    frozen_json_mapping,
    frozen_mapping,
    require_digest,
    require_id,
    require_text,
    require_unique,
    require_utc,
)

__all__ = [
    "DIGEST_PATTERN",
    "ID_PATTERN",
    "frozen_json_mapping",
    "frozen_mapping",
    "require_digest",
    "require_id",
    "require_text",
    "require_unique",
    "require_utc",
]

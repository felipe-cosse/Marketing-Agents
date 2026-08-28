"""Shared bounded projection for untrusted connector result metadata."""

from __future__ import annotations

from collections.abc import Mapping

from marketing_agents.domain.runtime_policy import canonical_payload_size_bytes

OUTPUT_PROJECTION_OMITTED = {"omitted": "output_payload_too_large"}


def bounded_connector_output_projection(
    safe_metadata: object,
    max_output_bytes: int,
) -> Mapping[str, object]:
    """Retain canonical metadata only when it fits the sealed step budget."""

    if isinstance(safe_metadata, Mapping):
        try:
            if canonical_payload_size_bytes(safe_metadata) <= max_output_bytes:
                return safe_metadata
        except Exception:
            # Connector result objects are outside the trust boundary. Treat an
            # uncanonicalizable projection exactly like an oversized projection.
            pass
    if canonical_payload_size_bytes(OUTPUT_PROJECTION_OMITTED) <= max_output_bytes:
        return OUTPUT_PROJECTION_OMITTED
    # Empty metadata is the domain representation of no retained output. Receipt
    # identity and status remain exact control-plane evidence.
    return {}

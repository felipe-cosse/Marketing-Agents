"""Stable domain-separated external-action idempotency keys."""

from __future__ import annotations

import hashlib

from marketing_agents.domain.action_hash import ExternalActionKeyMaterial
from marketing_agents.domain.canonical_json import canonical_json_bytes

ACTION_IDEMPOTENCY_KEY_DOMAIN = b"marketing-agents:external-action-idempotency-key:v1\x00"
# Compatibility alias kept deliberately private to RUN-05 callers; the public name
# makes the hash domain explicit beside RUN-02's action-hash domains.
ACTION_IDEMPOTENCY_DOMAIN = ACTION_IDEMPOTENCY_KEY_DOMAIN
ACTION_IDEMPOTENCY_PREFIX = "action-idempotency-v1:"


def derive_external_action_idempotency_key(material: ExternalActionKeyMaterial) -> str:
    """Derive the restart-stable provider key from all seven stable fields."""

    projection = {
        "run_id": material.run_id,
        "plan_hash": material.plan_hash,
        "proposal_revision": material.proposal_revision,
        "step_key": material.step_key,
        "action_type": material.action_type,
        "binding_id": material.binding_id,
        "semantic_action_hash": material.semantic_action_hash,
    }
    digest = hashlib.sha256(
        ACTION_IDEMPOTENCY_KEY_DOMAIN + canonical_json_bytes(projection)
    ).hexdigest()
    return f"{ACTION_IDEMPOTENCY_PREFIX}{digest}"

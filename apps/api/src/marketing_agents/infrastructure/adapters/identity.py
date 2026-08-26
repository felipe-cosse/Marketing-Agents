"""Credential-free local identity adapter with a fixed server-owned actor."""

from __future__ import annotations

import hashlib
import ipaddress

from marketing_agents.application.ports.identity import (
    AuthenticationEvidence,
    IdentityAuthenticationError,
)
from marketing_agents.application.ports.webhooks import (
    require_webhook_source_id,
    require_webhook_trigger_id,
)
from marketing_agents.config import Settings
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.identity import (
    AuthenticatedPrincipal,
    AuthenticationMethod,
    PrincipalKind,
    _issue_authenticated_principal,
)

_WEBHOOK_ACTOR_ID_DOMAIN = b"marketing-agents:webhook-service-actor:v1\x00"


def issue_verified_webhook_principal(
    *,
    source: str,
    trigger_id: str,
) -> AuthenticatedPrincipal:
    """Issue least-privilege service authority for one authenticated binding."""

    require_webhook_source_id(source, "verified webhook principal source")
    require_webhook_trigger_id(trigger_id, "verified webhook principal trigger ID")
    actor_digest = hashlib.sha256(
        _WEBHOOK_ACTOR_ID_DOMAIN
        + canonical_json_bytes({"source": source, "trigger_id": trigger_id})
    ).hexdigest()
    return _issue_authenticated_principal(
        actor_id=f"webhook-service:{actor_digest}",
        kind=PrincipalKind.SERVICE,
        authentication_method=AuthenticationMethod.VERIFIED_WEBHOOK,
        roles=frozenset({"webhook_service"}),
        scopes=frozenset(
            {
                "webhook:submit",
                f"webhook:source:{source}",
                f"webhook:trigger:{trigger_id}",
            }
        ),
    )


class LocalIdentityProvider:
    """Authenticate only absent credentials under validated local-only settings."""

    def __init__(self, settings: Settings) -> None:
        if type(settings) is not Settings:
            raise ValueError("local identity requires exact validated settings")
        if (
            settings.auth_mode != "local"
            or settings.app_env == "production"
            or not ipaddress.ip_address(settings.api_host).is_loopback
        ):
            raise ValueError("local identity requires non-production loopback-only settings")
        self._principal = _issue_authenticated_principal(
            actor_id=settings.local_identity_actor_id,
            kind=PrincipalKind.HUMAN,
            authentication_method=AuthenticationMethod.LOCAL_FIXED,
            roles=frozenset(settings.local_identity_roles),
            scopes=frozenset(settings.local_identity_scopes),
        )

    async def authenticate(
        self,
        evidence: AuthenticationEvidence,
    ) -> AuthenticatedPrincipal:
        if type(evidence) is not AuthenticationEvidence:
            raise IdentityAuthenticationError("authentication_evidence_invalid")
        if evidence.bearer_token is not None:
            raise IdentityAuthenticationError("local_bearer_forbidden")
        self._principal.verify_integrity()
        return self._principal

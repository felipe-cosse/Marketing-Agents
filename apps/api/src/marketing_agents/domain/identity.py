"""Integrity-checked principal snapshots issued only by identity adapters."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum

from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.validation import require_id

_PRINCIPAL_SEAL = object()
_PRINCIPAL_FINGERPRINT_DOMAIN = b"marketing-agents:authenticated-principal:v1\x00"


class PrincipalKind(StrEnum):
    HUMAN = "human"
    SERVICE = "service"


class AuthenticationMethod(StrEnum):
    LOCAL_FIXED = "local_fixed"
    BEARER = "bearer"
    VERIFIED_WEBHOOK = "verified_webhook"
    INTERNAL_SCHEDULER = "internal_scheduler"


@dataclass(frozen=True, slots=True, init=False)
class AuthenticatedPrincipal:
    """Adapter-issued authority that cannot be constructed from transport claims."""

    actor_id: str
    kind: PrincipalKind
    authentication_method: AuthenticationMethod
    roles: frozenset[str] = field(repr=False)
    scopes: frozenset[str] = field(repr=False)
    issuance_fingerprint: str = field(repr=False)

    def __init__(
        self,
        *,
        actor_id: str,
        kind: PrincipalKind,
        authentication_method: AuthenticationMethod,
        roles: frozenset[str],
        scopes: frozenset[str],
        _seal: object,
    ) -> None:
        if _seal is not _PRINCIPAL_SEAL:
            raise ValueError("authenticated principals must be issued by an identity adapter")
        object.__setattr__(self, "actor_id", actor_id)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "authentication_method", authentication_method)
        object.__setattr__(self, "roles", roles)
        object.__setattr__(self, "scopes", scopes)
        self._validate()
        object.__setattr__(self, "issuance_fingerprint", _principal_fingerprint(self))

    def _validate(self) -> None:
        require_id(self.actor_id, "authenticated actor ID")
        if type(self.kind) is not PrincipalKind:
            raise ValueError("authenticated principal kind must use the exact enum")
        if type(self.authentication_method) is not AuthenticationMethod:
            raise ValueError("authentication method must use the exact enum")
        for values, name in ((self.roles, "principal roles"), (self.scopes, "principal scopes")):
            if type(values) is not frozenset or any(type(value) is not str for value in values):
                raise ValueError(f"{name} must be an exact immutable string set")
            for value in values:
                require_id(value, name)
        if not self.roles:
            raise ValueError("an authenticated principal must retain at least one role")
        human_methods = {AuthenticationMethod.LOCAL_FIXED, AuthenticationMethod.BEARER}
        service_methods = {
            AuthenticationMethod.VERIFIED_WEBHOOK,
            AuthenticationMethod.INTERNAL_SCHEDULER,
        }
        if (
            self.kind is PrincipalKind.HUMAN and self.authentication_method not in human_methods
        ) or (
            self.kind is PrincipalKind.SERVICE and self.authentication_method not in service_methods
        ):
            raise ValueError("principal kind and authentication method disagree")

    def verify_integrity(self) -> None:
        self._validate()
        if self.issuance_fingerprint != _principal_fingerprint(self):
            raise ValueError("authenticated principal changed after adapter issuance")


def _issue_authenticated_principal(
    *,
    actor_id: str,
    kind: PrincipalKind,
    authentication_method: AuthenticationMethod,
    roles: frozenset[str],
    scopes: frozenset[str],
) -> AuthenticatedPrincipal:
    """Private issuance seam imported only by trusted infrastructure adapters."""

    return AuthenticatedPrincipal(
        actor_id=actor_id,
        kind=kind,
        authentication_method=authentication_method,
        roles=roles,
        scopes=scopes,
        _seal=_PRINCIPAL_SEAL,
    )


def _principal_fingerprint(principal: AuthenticatedPrincipal) -> str:
    material = {
        "actor_id": principal.actor_id,
        "authentication_method": principal.authentication_method.value,
        "kind": principal.kind.value,
        "roles": sorted(principal.roles),
        "scopes": sorted(principal.scopes),
    }
    return hashlib.sha256(
        _PRINCIPAL_FINGERPRINT_DOMAIN + canonical_json_bytes(material)
    ).hexdigest()

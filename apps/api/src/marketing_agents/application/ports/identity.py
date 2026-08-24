"""Transport-independent identity evidence and authentication port."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from pydantic import SecretStr

from marketing_agents.domain.identity import AuthenticatedPrincipal


@dataclass(frozen=True, slots=True)
class AuthenticationEvidence:
    """Minimal credential evidence; bearer material is excluded from representation."""

    bearer_token: SecretStr | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.bearer_token is not None and type(self.bearer_token) is not SecretStr:
            raise ValueError("bearer evidence must use the secret wrapper")

    def safe_snapshot(self) -> dict[str, bool]:
        return {"bearer_present": self.bearer_token is not None}


class IdentityAuthenticationError(PermissionError):
    """Typed authentication denial with no credential-bearing message."""

    def __init__(self, code: str, message: str = "authentication failed") -> None:
        super().__init__(message)
        self.code = code


class IdentityProvider(Protocol):
    async def authenticate(
        self,
        evidence: AuthenticationEvidence,
    ) -> AuthenticatedPrincipal: ...

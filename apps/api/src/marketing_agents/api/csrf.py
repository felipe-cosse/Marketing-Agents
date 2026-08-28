"""Single-process local-v1 CSRF token provider."""

from __future__ import annotations

import secrets


class ProcessLocalCsrfToken:
    """Hold one per-start token without exposing it through object representations."""

    __slots__ = ("_token",)

    def __init__(self) -> None:
        self._token = secrets.token_urlsafe(32)

    def token_for_same_origin_session(self) -> str:
        return self._token

    def matches(self, candidate: str) -> bool:
        return type(candidate) is str and secrets.compare_digest(candidate, self._token)

    def __repr__(self) -> str:
        return "ProcessLocalCsrfToken([REDACTED])"


__all__ = ["ProcessLocalCsrfToken"]

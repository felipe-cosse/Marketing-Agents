"""Server-owned request correlation shared by transport and route commands."""

from __future__ import annotations

import re
import secrets
from typing import cast

from fastapi import Request
from starlette.types import Scope

CORRELATION_HEADER = "X-Correlation-ID"
_CORRELATION_STATE_KEY = "correlation_id"
_CORRELATION_PATTERN = re.compile(r"^correlation\.api\.[0-9a-f]{32}$")


def new_correlation_id() -> str:
    """Issue an unpredictable identifier that is never accepted from a caller."""

    return f"correlation.api.{secrets.token_hex(16)}"


def install_scope_correlation(scope: Scope, correlation_id: str) -> Scope:
    """Return a shallow scope copy containing authoritative request state."""

    if not _CORRELATION_PATTERN.fullmatch(correlation_id):
        raise ValueError("invalid server correlation identifier")
    secured_scope = cast(Scope, dict(scope))
    state = scope.get("state")
    secured_state = dict(state) if isinstance(state, dict) else {}
    secured_state[_CORRELATION_STATE_KEY] = correlation_id
    secured_scope["state"] = secured_state
    return secured_scope


def scope_correlation_id(scope: Scope) -> str:
    state = scope.get("state")
    correlation_id = state.get(_CORRELATION_STATE_KEY) if isinstance(state, dict) else None
    if type(correlation_id) is not str or not _CORRELATION_PATTERN.fullmatch(correlation_id):
        raise RuntimeError("request correlation middleware is not installed")
    return correlation_id


def request_correlation_id(request: Request) -> str:
    """Resolve the exact server correlation for an application command."""

    return scope_correlation_id(request.scope)


__all__ = [
    "CORRELATION_HEADER",
    "install_scope_correlation",
    "new_correlation_id",
    "request_correlation_id",
    "scope_correlation_id",
]

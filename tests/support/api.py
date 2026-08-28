"""API test helpers that exercise the real process-local browser contract."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from httpx import ASGITransport, AsyncClient, Response

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_CORRELATION_PATTERN = re.compile(r"^correlation\.api\.[0-9a-f]{32}$")


def _header_items(headers: object) -> list[tuple[str | bytes, str | bytes]]:
    if headers is None:
        return []
    multi_items = getattr(headers, "multi_items", None)
    if callable(multi_items):
        return list(multi_items())
    if isinstance(headers, Mapping):
        return list(headers.items())
    if isinstance(headers, Sequence) and not isinstance(headers, (str, bytes, bytearray)):
        return list(headers)
    raise TypeError("test request headers must be a mapping or pair sequence")


def _direct_test_token(application: object | None) -> str | None:
    state = getattr(application, "state", None)
    provider = getattr(state, "csrf_token", None)
    issue = getattr(provider, "token_for_same_origin_session", None)
    if not callable(issue):
        return None
    token = issue()
    return token if type(token) is str else None


async def browser_request(
    client: AsyncClient,
    method: str,
    path: str,
    *,
    csrf_app: object | None = None,
    **kwargs: Any,
) -> Response:
    """Send one request, obtaining a real session token for control-plane mutations."""

    upper_method = method.upper()
    if (
        upper_method not in _UNSAFE_METHODS
        or "/api/v1/" not in path
        or "/api/v1/webhooks/" in path
    ):
        return await client.request(method, path, **kwargs)

    session_prefix = path.split("/api/v1/", 1)[0]
    session = await client.get(f"{session_prefix}/api/v1/session")
    token = session.json().get("csrfToken") if session.status_code == 200 else None
    if type(token) is not str:
        token = _direct_test_token(csrf_app)
    if type(token) is not str:
        raise AssertionError("test mutation could not obtain a process-local CSRF token")

    supplied = _header_items(kwargs.pop("headers", None))
    supplied_names = {
        name.casefold() if isinstance(name, str) else name.lower().decode("ascii", "ignore")
        for name, _value in supplied
    }
    browser_headers = [
        (name, value)
        for name, value in (
            ("Origin", "http://testserver"),
            ("Sec-Fetch-Site", "same-origin"),
            ("X-CSRF-Token", token),
        )
        if name.casefold() not in supplied_names
    ]
    return await client.request(
        method,
        path,
        headers=[*browser_headers, *supplied],
        **kwargs,
    )


async def api_request(
    application: object,
    method: str,
    path: str,
    **kwargs: Any,
) -> Response:
    async with AsyncClient(
        transport=ASGITransport(app=application),  # type: ignore[arg-type]
        base_url="http://testserver",
    ) as client:
        return await browser_request(
            client,
            method,
            path,
            csrf_app=application,
            **kwargs,
        )


def assert_problem(response: Response, *, status_code: int, code: str) -> dict[str, Any]:
    """Assert the shared API-09 occurrence envelope and return optional fields."""

    assert response.status_code == status_code
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["cache-control"] == "no-store"
    correlation_id = response.headers["x-correlation-id"]
    assert _CORRELATION_PATTERN.fullmatch(correlation_id)
    payload = response.json()
    assert payload["status"] == status_code
    assert payload["code"] == code
    assert payload["correlation_id"] == correlation_id
    assert payload["instance"] == f"urn:marketing-agents:request:{correlation_id}"
    assert payload["type"] == f"urn:marketing-agents:problem:{code}"
    return payload


__all__ = ["api_request", "assert_problem", "browser_request"]

"""API-09 pure-ASGI transport, browser-security, timeout, and error boundary."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
from collections.abc import Iterable, Mapping
from urllib.parse import SplitResult, urlsplit

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from marketing_agents.api.correlation import (
    CORRELATION_HEADER,
    install_scope_correlation,
    new_correlation_id,
)
from marketing_agents.api.csrf import ProcessLocalCsrfToken
from marketing_agents.api.errors import PROBLEM_MEDIA_TYPE, problem_details, status_defaults
from marketing_agents.api.schemas.problems import ProblemFieldError
from marketing_agents.api.strict_json import (
    strict_json_route_path,
    strict_json_transport_headers_are_valid,
)
from marketing_agents.config import Settings

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_DNS_LABEL_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_MAX_ERROR_BODY_BYTES = 65_536
_PROBLEM_PRESERVED_HEADERS = frozenset(
    {b"allow", b"etag", b"retry-after", b"vary", b"www-authenticate"}
)
_SECURITY_HEADER_NAMES = frozenset(
    {
        b"content-security-policy",
        b"referrer-policy",
        b"x-content-type-options",
        b"x-frame-options",
        CORRELATION_HEADER.lower().encode("ascii"),
    }
)
_SECURITY_HEADERS = (
    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'; base-uri 'none'"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
)


def _normalize_hostname(value: str) -> str:
    if not value or len(value) > 253 or value.endswith("."):
        raise ValueError("invalid host")
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        try:
            ascii_value = value.encode("ascii", errors="strict").decode("ascii").casefold()
        except UnicodeError:
            raise ValueError("invalid host") from None
        labels = ascii_value.split(".")
        if any(not _DNS_LABEL_PATTERN.fullmatch(label) for label in labels):
            raise ValueError("invalid host") from None
        return ascii_value
    return address.compressed.casefold()


def _validated_port(value: str) -> int:
    if not value or not value.isascii() or not value.isdigit():
        raise ValueError("invalid port")
    port = int(value)
    if not 1 <= port <= 65_535:
        raise ValueError("invalid port")
    return port


def _parse_host_header(value: bytes) -> str:
    try:
        authority = value.decode("ascii", errors="strict")
    except UnicodeDecodeError:
        raise ValueError("invalid host") from None
    if (
        not authority
        or len(authority) > 512
        or any(ord(character) <= 0x20 or ord(character) == 0x7F for character in authority)
        or any(character in authority for character in "/\\?#@,")
    ):
        raise ValueError("invalid host")
    if authority.startswith("["):
        closing = authority.find("]")
        if closing < 2:
            raise ValueError("invalid host")
        host = authority[1:closing]
        suffix = authority[closing + 1 :]
        if suffix:
            if not suffix.startswith(":") or suffix.count(":") != 1:
                raise ValueError("invalid host")
            _validated_port(suffix[1:])
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            raise ValueError("invalid host") from None
        if not isinstance(address, ipaddress.IPv6Address):
            raise ValueError("invalid host")
        return address.compressed.casefold()
    if authority.count(":") > 1:
        raise ValueError("invalid host")
    host, separator, raw_port = authority.partition(":")
    if separator:
        _validated_port(raw_port)
    return _normalize_hostname(host)


def _validated_origin_parts(origin: str) -> tuple[SplitResult, str, int | None]:
    if (
        type(origin) is not str
        or not origin
        or len(origin) > 512
        or any(ord(character) <= 0x20 or ord(character) == 0x7F for character in origin)
    ):
        raise ValueError("invalid origin")
    try:
        origin.encode("ascii", errors="strict")
        parsed = urlsplit(origin)
        port = parsed.port
    except (UnicodeError, ValueError):
        raise ValueError("invalid origin") from None
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.netloc
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
        or parsed.hostname is None
    ):
        raise ValueError("invalid origin")
    if port is not None and not 1 <= port <= 65_535:
        raise ValueError("invalid origin")
    return parsed, _normalize_hostname(parsed.hostname), port


def _canonical_origin(origin: str) -> str:
    parsed, host, port = _validated_origin_parts(origin)
    scheme = parsed.scheme.casefold()
    if (scheme, port) in {("http", 80), ("https", 443)}:
        port = None
    authority = f"[{host}]" if ":" in host else host
    if port is not None:
        authority = f"{authority}:{port}"
    return f"{scheme}://{authority}"


def _headers(scope: Scope, name: bytes) -> list[bytes]:
    return [value for raw_name, value in scope.get("headers", ()) if raw_name.lower() == name]


def _is_exact_webhook_route(path: str) -> bool:
    parts = path.split("/")
    return len(parts) == 6 and parts[:4] == ["", "api", "v1", "webhooks"] and all(parts[4:])


def _secured_response_headers(
    headers: Iterable[tuple[bytes, bytes]],
    correlation_id: str,
) -> list[tuple[bytes, bytes]]:
    result = [
        (name, value) for name, value in headers if name.lower() not in _SECURITY_HEADER_NAMES
    ]
    result.extend(_SECURITY_HEADERS)
    result.append((CORRELATION_HEADER.lower().encode("ascii"), correlation_id.encode("ascii")))
    return result


def _safe_problem_code(payload: object, fallback: str) -> str:
    candidate: object = None
    if isinstance(payload, Mapping):
        candidate = payload.get("code")
        detail = payload.get("detail")
        if candidate is None and isinstance(detail, Mapping):
            candidate = detail.get("code")
    return candidate if type(candidate) is str and _CODE_PATTERN.fullmatch(candidate) else fallback


def _safe_field_errors(payload: object) -> tuple[ProblemFieldError, ...] | None:
    if not isinstance(payload, Mapping):
        return None
    raw_errors: object = payload.get("field_errors")
    detail = payload.get("detail")
    if raw_errors is None and isinstance(detail, Mapping):
        raw_errors = detail.get("field_errors")
    pointer = payload.get("pointer")
    if raw_errors is None and type(pointer) is str:
        raw_errors = [
            {
                "pointer": pointer,
                "code": _safe_problem_code(payload, "invalid_value"),
            }
        ]
    if not isinstance(raw_errors, list):
        return None
    safe_errors: list[ProblemFieldError] = []
    for item in raw_errors[:32]:
        if not isinstance(item, Mapping):
            continue
        pointer = item.get("pointer")
        if type(pointer) is str and re.fullmatch(
            r"/input(?:/[A-Za-z0-9_.-]{1,100}){0,64}",
            pointer,
        ):
            safe_pointer = pointer
        else:
            root = (
                pointer.split("/", 2)[1]
                if type(pointer) is str and pointer.startswith("/")
                else "request"
            )
            if root not in {"body", "path", "query", "header", "cookie", "request"}:
                root = "request"
            safe_pointer = f"/{root}"
        code = item.get("code")
        safe_errors.append(
            ProblemFieldError(
                pointer=safe_pointer,
                code=(
                    code if type(code) is str and _CODE_PATTERN.fullmatch(code) else "invalid_value"
                ),
                message="invalid request field",
            )
        )
    return tuple(safe_errors) or None


def _safe_optional_problem_fields(
    payload: object,
    headers: Iterable[tuple[bytes, bytes]],
) -> tuple[int | None, int | str | None]:
    retry_after: int | None = None
    for name, value in headers:
        if name.lower() == b"retry-after":
            try:
                candidate = int(value.decode("ascii", errors="strict"))
            except (UnicodeError, ValueError):
                continue
            if 0 <= candidate <= 86_400:
                retry_after = candidate
                break
    current: object = None
    if isinstance(payload, Mapping):
        detail = payload.get("detail")
        for container in (payload, detail if isinstance(detail, Mapping) else {}):
            for key in (
                "current_resource_version",
                "currentResourceVersion",
                "current_revision",
                "currentRevision",
            ):
                if key in container:
                    current = container[key]
                    break
            if current is not None:
                break
    if type(current) is int and 0 <= current <= 2**63 - 1:
        safe_current: int | str | None = current
    elif type(current) is str and _CODE_PATTERN.fullmatch(current):
        safe_current = current
    else:
        safe_current = None
    return retry_after, safe_current


def _decode_error_payload(body: bytes) -> object:
    if not body or len(body) > _MAX_ERROR_BODY_BYTES:
        return None
    try:
        return json.loads(body.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        return None


def _problem_vary_headers(
    scope: Scope,
    *,
    include_origin: bool = False,
) -> tuple[tuple[bytes, bytes], ...]:
    path = strict_json_route_path(scope)
    if not (path == "/api/v1" or path.startswith("/api/v1/")):
        return ()
    vary = (
        b"Authorization, Origin"
        if include_origin or path == "/api/v1/session"
        else b"Authorization"
    )
    return ((b"vary", vary),)


def _preserved_problem_headers(
    headers: Iterable[tuple[bytes, bytes]],
) -> list[tuple[bytes, bytes]]:
    unique: dict[bytes, tuple[bytes, bytes]] = {}
    vary_tokens: list[bytes] = []
    vary_seen: set[bytes] = set()
    for name, value in headers:
        lowered = name.lower()
        if lowered not in _PROBLEM_PRESERVED_HEADERS:
            continue
        if lowered == b"vary":
            for token in value.split(b","):
                normalized = token.strip()
                key = normalized.lower()
                if normalized and key not in vary_seen:
                    vary_seen.add(key)
                    vary_tokens.append(normalized)
            continue
        unique[lowered] = (name, value)
    result = list(unique.values())
    if vary_tokens:
        result.append((b"vary", b", ".join(vary_tokens)))
    return result


async def _send_problem(
    send: Send,
    *,
    correlation_id: str,
    status_code: int,
    code: str | None = None,
    title: str | None = None,
    detail: str | None = None,
    field_errors: tuple[ProblemFieldError, ...] | None = None,
    retry_after_seconds: int | None = None,
    current_resource_version: int | str | None = None,
    retained_headers: Iterable[tuple[bytes, bytes]] = (),
) -> None:
    problem = problem_details(
        status_code=status_code,
        correlation_id=correlation_id,
        code=code,
        title=title,
        detail=detail,
        field_errors=field_errors,
        retry_after_seconds=retry_after_seconds,
        current_resource_version=current_resource_version,
    )
    body = problem.model_dump_json(exclude_none=True).encode("utf-8")
    preserved = _preserved_problem_headers(retained_headers)
    headers = [
        *preserved,
        (b"cache-control", b"no-store"),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"content-type", PROBLEM_MEDIA_TYPE.encode("ascii")),
    ]
    await send(
        {
            "type": "http.response.start",
            "status": problem.status,
            "headers": _secured_response_headers(headers, correlation_id),
        }
    )
    await send({"type": "http.response.body", "body": body})


class _ResponseBoundary:
    """Stream successes and hold errors until they can be normalized safely."""

    def __init__(
        self,
        send: Send,
        correlation_id: str,
        *,
        normalize_errors: bool,
        problem_headers: tuple[tuple[bytes, bytes], ...],
    ) -> None:
        self._send = send
        self._correlation_id = correlation_id
        self._normalize_errors = normalize_errors
        self._problem_headers = problem_headers
        self._status_code: int | None = None
        self._headers: list[tuple[bytes, bytes]] = []
        self._error_body = bytearray()
        self._error_body_overflow = False
        self.sent_start = False
        self.completed = False

    async def __call__(self, message: Message) -> None:
        message_type = message.get("type")
        if message_type == "http.response.start":
            if self._status_code is not None:
                raise RuntimeError("downstream sent duplicate response start")
            self._status_code = int(message["status"])
            self._headers = list(message.get("headers", ()))
            if self._status_code < 400 or not self._normalize_errors:
                secured = dict(message)
                secured["headers"] = _secured_response_headers(
                    self._headers,
                    self._correlation_id,
                )
                await self._send(secured)
                self.sent_start = True
            return
        if message_type != "http.response.body":
            await self._send(message)
            return
        if self._status_code is None:
            raise RuntimeError("downstream sent response body before response start")
        if self._status_code < 400 or not self._normalize_errors:
            await self._send(message)
            if not message.get("more_body", False):
                self.completed = True
            return
        body = message.get("body", b"")
        if not isinstance(body, bytes):
            raise RuntimeError("downstream response body must be bytes")
        if not self._error_body_overflow:
            if len(self._error_body) + len(body) <= _MAX_ERROR_BODY_BYTES:
                self._error_body.extend(body)
            else:
                self._error_body_overflow = True
                self._error_body.clear()
        if message.get("more_body", False):
            return
        await self._finalize_error()

    async def _finalize_error(self) -> None:
        if self._status_code is None:
            raise RuntimeError("cannot finalize a response without status")
        payload = (
            None if self._error_body_overflow else _decode_error_payload(bytes(self._error_body))
        )
        _title, fallback_code, _detail = status_defaults(self._status_code)
        retry_after, current = _safe_optional_problem_fields(payload, self._headers)
        await _send_problem(
            self._send,
            correlation_id=self._correlation_id,
            status_code=self._status_code,
            code=_safe_problem_code(payload, fallback_code),
            field_errors=_safe_field_errors(payload),
            retry_after_seconds=retry_after,
            current_resource_version=current,
            retained_headers=[*self._problem_headers, *self._headers],
        )
        self.sent_start = True
        self.completed = True


class Api09TransportSecurityMiddleware:
    """Own the complete request lifetime before any route-local middleware."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        settings: Settings,
        csrf_token: ProcessLocalCsrfToken,
    ) -> None:
        if type(settings) is not Settings or type(csrf_token) is not ProcessLocalCsrfToken:
            raise ValueError("API-09 middleware requires validated local process state")
        self._app = app
        self._timeout_seconds = settings.api_request_timeout_seconds
        self._trusted_hosts = frozenset(
            _normalize_hostname(host[1:-1] if host.startswith("[") and host.endswith("]") else host)
            for host in settings.trusted_hosts
        )
        self._trusted_origins = frozenset(
            _canonical_origin(origin) for origin in settings.trusted_origins
        )
        self._csrf_token = csrf_token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        correlation_id = new_correlation_id()
        secured_scope = install_scope_correlation(scope, correlation_id)
        secured_scope["headers"] = tuple(
            (name, value)
            for name, value in scope.get("headers", ())
            if name.lower() != CORRELATION_HEADER.lower().encode("ascii")
        )
        rejection = self._transport_rejection(secured_scope)
        if rejection is not None:
            status_code, code = rejection
            await _send_problem(
                send,
                correlation_id=correlation_id,
                status_code=status_code,
                code=code,
                retained_headers=_problem_vary_headers(
                    secured_scope,
                    include_origin=code in {"browser_request_forbidden", "csrf_token_invalid"},
                ),
            )
            return

        boundary = _ResponseBoundary(
            send,
            correlation_id,
            normalize_errors=strict_json_route_path(secured_scope) != "/health/ready",
            problem_headers=_problem_vary_headers(secured_scope),
        )
        try:
            async with asyncio.timeout(self._timeout_seconds):
                await self._app(secured_scope, receive, boundary)
        except TimeoutError:
            if boundary.sent_start:
                raise
            await _send_problem(
                send,
                correlation_id=correlation_id,
                status_code=503,
                code="request_timeout",
                title="Request Timeout",
                detail="The request exceeded its server time limit.",
                retained_headers=_problem_vary_headers(secured_scope),
            )
            return
        except Exception:
            if boundary.sent_start:
                raise
            await _send_problem(
                send,
                correlation_id=correlation_id,
                status_code=500,
                code="internal_server_error",
                retained_headers=_problem_vary_headers(secured_scope),
            )
            return
        if not boundary.completed:
            if boundary.sent_start:
                raise RuntimeError("downstream ended an incomplete response")
            await _send_problem(
                send,
                correlation_id=correlation_id,
                status_code=500,
                code="internal_server_error",
                retained_headers=_problem_vary_headers(secured_scope),
            )

    def _transport_rejection(self, scope: Scope) -> tuple[int, str] | None:
        host_values = _headers(scope, b"host")
        if len(host_values) != 1:
            return 400, "host_header_invalid"
        try:
            host = _parse_host_header(host_values[0])
        except ValueError:
            return 400, "host_header_invalid"
        if host not in self._trusted_hosts:
            return 400, "host_header_invalid"
        if any(
            name.lower() == b"forwarded" or name.lower().startswith(b"x-forwarded-")
            for name, _value in scope.get("headers", ())
        ):
            return 400, "forwarded_header_forbidden"

        method = scope.get("method", "").upper()
        path = strict_json_route_path(scope)
        if (
            method not in _UNSAFE_METHODS
            or not (path == "/api/v1" or path.startswith("/api/v1/"))
            or _is_exact_webhook_route(path)
        ):
            return None
        if not strict_json_transport_headers_are_valid(scope):
            return 403, "browser_request_forbidden"
        origins = _headers(scope, b"origin")
        fetch_sites = _headers(scope, b"sec-fetch-site")
        if len(origins) != 1 or len(fetch_sites) != 1:
            return 403, "browser_request_forbidden"
        try:
            origin = _canonical_origin(origins[0].decode("ascii", errors="strict"))
            fetch_site = fetch_sites[0].decode("ascii", errors="strict").casefold()
        except (UnicodeError, ValueError):
            return 403, "browser_request_forbidden"
        if origin not in self._trusted_origins or fetch_site != "same-origin":
            return 403, "browser_request_forbidden"
        csrf_values = _headers(scope, b"x-csrf-token")
        if len(csrf_values) != 1:
            return 403, "browser_request_forbidden"
        try:
            csrf_candidate = csrf_values[0].decode("ascii", errors="strict")
        except UnicodeError:
            return 403, "csrf_token_invalid"
        if len(csrf_candidate) > 128 or not self._csrf_token.matches(csrf_candidate):
            return 403, "csrf_token_invalid"
        return None


__all__ = ["Api09TransportSecurityMiddleware"]

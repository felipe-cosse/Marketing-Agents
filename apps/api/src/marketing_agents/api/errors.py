"""Safe, process-wide Problem Details construction and exception projection."""

from __future__ import annotations

import re
from collections.abc import Mapping

from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from marketing_agents.api.correlation import request_correlation_id
from marketing_agents.api.schemas.problems import ProblemDetails, ProblemFieldError

PROBLEM_MEDIA_TYPE = "application/problem+json"
_SAFE_LOCATION_ROOTS = frozenset({"body", "path", "query", "header", "cookie"})
_MAX_FIELD_ERRORS = 32
_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_HTTP_EXCEPTION_HEADER_ALLOWLIST = frozenset({"allow", "etag", "retry-after", "www-authenticate"})

_STATUS_DEFAULTS: dict[int, tuple[str, str, str]] = {
    400: ("Bad Request", "request_invalid", "The request transport is invalid."),
    401: ("Unauthorized", "authentication_required", "Valid authentication is required."),
    403: ("Forbidden", "request_forbidden", "The request is not authorized."),
    404: ("Not Found", "resource_not_found", "The requested resource was not found."),
    405: ("Method Not Allowed", "method_not_allowed", "The method is not allowed."),
    409: ("Conflict", "resource_conflict", "The request conflicts with current state."),
    413: ("Content Too Large", "payload_too_large", "The request payload is too large."),
    415: (
        "Unsupported Media Type",
        "media_type_unsupported",
        "The request media type is not supported.",
    ),
    422: (
        "Unprocessable Content",
        "request_validation_failed",
        "Request validation failed.",
    ),
    429: ("Too Many Requests", "rate_limit_exceeded", "The request rate limit was exceeded."),
    500: ("Internal Server Error", "internal_server_error", "The request could not be completed."),
    503: (
        "Service Unavailable",
        "service_unavailable",
        "The service is temporarily unavailable.",
    ),
}


def _pointer(location: tuple[object, ...]) -> str:
    root = location[0] if location else None
    return f"/{root}" if type(root) is str and root in _SAFE_LOCATION_ROOTS else "/request"


def _safe_code(candidate: object, *, fallback: str) -> str:
    return candidate if type(candidate) is str and _CODE_PATTERN.fullmatch(candidate) else fallback


def status_defaults(status_code: int) -> tuple[str, str, str]:
    """Return a bounded title, code, and detail for one HTTP status."""

    status_class = status_code if status_code in _STATUS_DEFAULTS else 500
    return _STATUS_DEFAULTS[status_class]


def problem_details(
    *,
    status_code: int,
    correlation_id: str,
    code: str | None = None,
    title: str | None = None,
    detail: str | None = None,
    field_errors: tuple[ProblemFieldError, ...] | None = None,
    retry_after_seconds: int | None = None,
    current_resource_version: int | str | None = None,
) -> ProblemDetails:
    """Build a strict occurrence document from server-owned values only."""

    default_title, default_code, default_detail = status_defaults(status_code)
    safe_code = _safe_code(code, fallback=default_code)
    return ProblemDetails(
        type=f"urn:marketing-agents:problem:{safe_code}",
        title=title or default_title,
        status=status_code if 400 <= status_code <= 599 else 500,
        detail=detail or default_detail,
        instance=f"urn:marketing-agents:request:{correlation_id}",
        code=safe_code,
        correlation_id=correlation_id,
        field_errors=field_errors,
        retry_after_seconds=retry_after_seconds,
        current_resource_version=current_resource_version,
    )


def problem_response(
    request: Request,
    *,
    status_code: int,
    code: str | None = None,
    field_errors: tuple[ProblemFieldError, ...] | None = None,
    retry_after_seconds: int | None = None,
    current_resource_version: int | str | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    problem = problem_details(
        status_code=status_code,
        correlation_id=request_correlation_id(request),
        code=code,
        field_errors=field_errors,
        retry_after_seconds=retry_after_seconds,
        current_resource_version=current_resource_version,
    )
    response_headers = {"Cache-Control": "no-store"}
    if headers is not None:
        response_headers.update(headers)
    return JSONResponse(
        status_code=problem.status,
        content=problem.model_dump(mode="json", exclude_none=True),
        media_type=PROBLEM_MEDIA_TYPE,
        headers=response_headers,
    )


def _http_problem_code(error: StarletteHTTPException, status_code: int) -> str | None:
    detail = error.detail
    if isinstance(detail, Mapping):
        _title, fallback, _safe_detail = status_defaults(status_code)
        return _safe_code(detail.get("code"), fallback=fallback)
    return None


def _http_current_resource_version(error: StarletteHTTPException) -> int | str | None:
    detail = error.detail
    if not isinstance(detail, Mapping):
        return None
    candidate = detail.get("current_resource_version")
    if type(candidate) is int and 0 <= candidate <= 2**63 - 1:
        return candidate
    if type(candidate) is str and _CODE_PATTERN.fullmatch(candidate):
        return candidate
    return None


async def safe_http_exception(
    request: Request,
    error: Exception,
) -> JSONResponse:
    """Map HTTP exceptions without reflecting their potentially attacker-derived detail."""

    if not isinstance(error, StarletteHTTPException):
        return problem_response(request, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
    headers = (
        {
            name: value
            for name, value in error.headers.items()
            if name.casefold() in _HTTP_EXCEPTION_HEADER_ALLOWLIST
            and type(value) is str
            and len(value) <= 1_000
            and all(ord(character) >= 0x20 and ord(character) != 0x7F for character in value)
        }
        if isinstance(error.headers, Mapping)
        else None
    )
    return problem_response(
        request,
        status_code=error.status_code,
        code=_http_problem_code(error, error.status_code),
        current_resource_version=_http_current_resource_version(error),
        headers=headers,
    )


async def safe_request_validation_error(
    request: Request,
    error: Exception,
) -> JSONResponse:
    """Return locations and stable codes only; never serialize input or validator context."""

    validation_errors = error.errors() if isinstance(error, RequestValidationError) else []
    field_errors = tuple(
        ProblemFieldError(
            pointer=_pointer(tuple(item.get("loc", ()))),
            code=_safe_code(item.get("type"), fallback="invalid_value"),
            message="invalid request field",
        )
        for item in validation_errors[:_MAX_FIELD_ERRORS]
    )
    return problem_response(
        request,
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        code="request_validation_failed",
        field_errors=field_errors,
    )


async def safe_unhandled_exception(
    request: Request,
    error: Exception,
) -> JSONResponse:
    """Never expose exception text or stack material to a caller."""

    del error
    return problem_response(request, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


__all__ = [
    "PROBLEM_MEDIA_TYPE",
    "problem_details",
    "problem_response",
    "safe_http_exception",
    "safe_request_validation_error",
    "safe_unhandled_exception",
    "status_defaults",
]

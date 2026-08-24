"""Safe transport error projections that never reflect request values."""

from __future__ import annotations

from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

_SAFE_LOCATION_ROOTS = frozenset({"body", "path", "query", "header", "cookie"})
_MAX_FIELD_ERRORS = 32


def _pointer(location: tuple[object, ...]) -> str:
    root = location[0] if location else None
    return f"/{root}" if type(root) is str and root in _SAFE_LOCATION_ROOTS else "/request"


async def safe_request_validation_error(
    request: Request,
    error: Exception,
) -> JSONResponse:
    """Return locations and stable codes only; never serialize input or validator context."""

    del request
    validation_errors = error.errors() if isinstance(error, RequestValidationError) else []
    field_errors = [
        {
            "pointer": _pointer(tuple(item.get("loc", ()))),
            "code": str(item.get("type", "invalid_value")),
            "message": "invalid request field",
        }
        for item in validation_errors[:_MAX_FIELD_ERRORS]
    ]
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={
            "detail": {
                "code": "request_validation_failed",
                "message": "request validation failed",
                "field_errors": field_errors,
            }
        },
    )

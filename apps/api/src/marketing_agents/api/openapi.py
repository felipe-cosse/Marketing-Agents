"""OpenAPI projection for the process-wide API-09 problem contract."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

_PROBLEM_SCHEMA = {"$ref": "#/components/schemas/ProblemDetails"}


def install_problem_openapi(application: FastAPI) -> None:
    """Rewrite every documented error response to the actual shared media type."""

    original_openapi = application.openapi

    def problem_openapi() -> dict[str, Any]:
        schema = original_openapi()
        paths = schema.get("paths")
        if not isinstance(paths, dict):
            return schema
        for path, path_item in paths.items():
            if not isinstance(path_item, dict):
                continue
            for operation in path_item.values():
                if not isinstance(operation, dict):
                    continue
                responses = operation.get("responses")
                if not isinstance(responses, dict):
                    continue
                for status_code, response in responses.items():
                    if not isinstance(response, dict):
                        continue
                    is_error = status_code == "default" or (
                        isinstance(status_code, str)
                        and status_code.isdigit()
                        and int(status_code) >= 400
                    )
                    if not is_error or (path == "/health/ready" and status_code == "503"):
                        continue
                    response["content"] = {
                        "application/problem+json": {"schema": dict(_PROBLEM_SCHEMA)}
                    }
        application.openapi_schema = schema
        return schema

    application.openapi = problem_openapi  # type: ignore[method-assign]


__all__ = ["install_problem_openapi"]

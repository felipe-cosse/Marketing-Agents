"""Fail-closed environment contracts for the AC-01 clean-checkout verifier.

The helpers intentionally compare complete environment maps.  Error messages are
fixed codes so a rejected credential or provider value never reaches diagnostics.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

BACKEND_SERVICES: Final = (
    "api",
    "run-worker",
    "scheduler-worker",
    "local-secret-init",
    "migrate-seed",
)
SERVICES: Final = (
    "api",
    "web",
    "run-worker",
    "scheduler-worker",
    "local-secret-init",
    "migrate-seed",
)
FIXTURE_SERVICES: Final = frozenset({"api", "run-worker", "scheduler-worker"})
PUBLIC_FIXTURE_HMAC: Final = "del-05-public-test-signing-material-never-use-as-a-secret"
API_SOCKET: Final = "/var/run/marketing-agents/api.sock"

_PROJECT_PATTERN: Final = re.compile(r"marketing-agents-del05-[0-9a-f]{16}\Z")
_ENVIRONMENT_NAME_PATTERN: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_IMAGE_ID_PATTERN: Final = re.compile(r"sha256:[0-9a-f]{64}\Z")

_BASE_BACKEND_COMPOSE_ENVIRONMENT: Final = {
    "APP_ENV": "local",
    "AUTH_MODE": "local",
    "LLM_PROVIDER": "mock",
    "CONNECTOR_MODE": "mock",
    "ALLOW_EXTERNAL_NETWORK": "false",
    "REAL_LLM_OPT_IN": "false",
    "REAL_CONNECTOR_OPT_IN": "false",
    "DATABASE_URL": "sqlite+aiosqlite:////var/lib/marketing-agents/data/marketing_agents.db",
    "MARKETING_AGENTS_DIGEST_KEY_PATH": "/var/lib/marketing-agents/secrets/digest.key",
    "CATALOG_ROOT": "/app/catalog/v1",
    "API_HOST": "127.0.0.1",
    "API_PORT": "8000",
}

# These maps are the exact inherited ENV from the pinned base image plus the ENV
# instructions in each tracked runtime stage.  Image inspection binds the values
# to an immutable image ID before any container is accepted.
_EXPECTED_IMAGE_ENVIRONMENTS: Final = {
    "backend": {
        "PATH": (
            "/app/.venv/bin:/usr/local/bin:/usr/local/sbin:/usr/local/bin:"
            "/usr/sbin:/usr/bin:/sbin:/bin"
        ),
        "LANG": "C.UTF-8",
        "GPG_KEY": "7169605F62C751356D054A26A821E680E5FA6305",
        "PYTHON_VERSION": "3.12.14",
        "PYTHON_SHA256": "5c8462af5790baf43a321a1559dbe0db06d1be4300fb85fb53c40060668e548a",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": "/app/apps/api/src:/app",
        "APP_ENV": "local",
        "AUTH_MODE": "local",
        "LLM_PROVIDER": "mock",
        "CONNECTOR_MODE": "mock",
        "ALLOW_EXTERNAL_NETWORK": "false",
        "REAL_LLM_OPT_IN": "false",
        "REAL_CONNECTOR_OPT_IN": "false",
        "DATABASE_URL": "sqlite+aiosqlite:////var/lib/marketing-agents/data/marketing_agents.db",
        "MARKETING_AGENTS_DIGEST_KEY_PATH": "/var/lib/marketing-agents/secrets/digest.key",
        "CATALOG_ROOT": "/app/catalog/v1",
    },
    "web": {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "NGINX_VERSION": "1.30.4",
        "PKG_RELEASE": "1",
        "DYNPKG_RELEASE": "1",
        "NJS_VERSION": "1.0.1",
        "NJS_RELEASE": "1",
    },
}


class EnvironmentBoundaryError(RuntimeError):
    """A value-redacted AC-01 environment boundary failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise EnvironmentBoundaryError(code)


@dataclass(frozen=True)
class ImageEnvironmentBaseline:
    """Verified immutable image identity and its exact safe environment."""

    image_id: str
    image_kind: str
    environment: tuple[tuple[str, str], ...]


def _valid_environment_text(value: str) -> bool:
    return not any(character in value for character in ("\x00", "\r", "\n"))


def _environment_entries(value: object, *, source: str) -> dict[str, str]:
    malformed = f"malformed_{source}_environment"
    duplicate = f"duplicate_{source}_environment"
    _require(isinstance(value, list), malformed)
    parsed: dict[str, str] = {}
    for entry in value:
        _require(
            isinstance(entry, str) and "=" in entry and _valid_environment_text(entry), malformed
        )
        name, entry_value = entry.split("=", 1)
        _require(bool(_ENVIRONMENT_NAME_PATTERN.fullmatch(name)), malformed)
        _require(name not in parsed, duplicate)
        parsed[name] = entry_value
    return parsed


def _environment_mapping(value: object) -> dict[str, str]:
    _require(isinstance(value, Mapping), "malformed_compose_environment")
    parsed: dict[str, str] = {}
    for name, entry_value in value.items():
        _require(
            isinstance(name, str)
            and bool(_ENVIRONMENT_NAME_PATTERN.fullmatch(name))
            and isinstance(entry_value, str)
            and _valid_environment_text(entry_value),
            "malformed_compose_environment",
        )
        parsed[name] = entry_value
    return parsed


def _validate_context(project: str, port: int, fixture_scope: str | None) -> None:
    _require(
        isinstance(project, str) and bool(_PROJECT_PATTERN.fullmatch(project)),
        "invalid_environment_project",
    )
    _require(
        isinstance(port, int) and not isinstance(port, bool) and 1024 <= port <= 65535,
        "invalid_environment_port",
    )
    _require(
        fixture_scope is None
        or (
            isinstance(fixture_scope, str)
            and bool(_PROJECT_PATTERN.fullmatch(fixture_scope))
            and fixture_scope == project
        ),
        "invalid_fixture_scope",
    )


def _expected_compose_environment(
    service: str, *, project: str, port: int, fixture_scope: str | None
) -> dict[str, str]:
    _validate_context(project, port, fixture_scope)
    _require(service in SERVICES, "unexpected_environment_service")
    if service == "web":
        return {}
    expected = dict(_BASE_BACKEND_COMPOSE_ENVIRONMENT)
    expected["API_TRUSTED_ORIGINS"] = json.dumps(
        [f"http://127.0.0.1:{port}", f"http://localhost:{port}"], separators=(",", ":")
    )
    if service == "api":
        expected["MARKETING_AGENTS_API_SOCKET"] = API_SOCKET
    if fixture_scope is not None and service in FIXTURE_SERVICES:
        expected["DEL05_VERIFICATION_SCOPE"] = fixture_scope
        expected["WEBHOOK_HMAC_SECRET"] = PUBLIC_FIXTURE_HMAC
    return expected


def verify_image_environment(
    inspected: Mapping[str, object], *, image_kind: str
) -> ImageEnvironmentBaseline:
    """Verify a built runtime image and return an immutable environment baseline."""

    _require(image_kind in _EXPECTED_IMAGE_ENVIRONMENTS, "unexpected_image_kind")
    _require(isinstance(inspected, Mapping), "malformed_image_inspection")
    image_id = inspected.get("Id")
    _require(
        isinstance(image_id, str) and bool(_IMAGE_ID_PATTERN.fullmatch(image_id)),
        "invalid_image_identity",
    )
    config = inspected.get("Config")
    _require(isinstance(config, Mapping), "malformed_image_inspection")
    actual = _environment_entries(config.get("Env"), source="image")
    _require(actual == _EXPECTED_IMAGE_ENVIRONMENTS[image_kind], "unsafe_image_environment")
    return ImageEnvironmentBaseline(
        image_id=image_id,
        image_kind=image_kind,
        environment=tuple(sorted(actual.items())),
    )


def validate_compose_service_environments(
    config: Mapping[str, object],
    *,
    project: str,
    port: int,
    fixture_scope: str | None = None,
) -> None:
    """Require the expanded Compose model to contain only the exact safe environment."""

    _validate_context(project, port, fixture_scope)
    _require(isinstance(config, Mapping), "malformed_compose_services")
    services = config.get("services")
    _require(
        isinstance(services, Mapping) and set(services) == set(SERVICES),
        "malformed_compose_services",
    )
    for service in SERVICES:
        service_config = services.get(service)
        _require(isinstance(service_config, Mapping), "malformed_compose_service")
        actual = _environment_mapping(service_config.get("environment", {}))
        expected = _expected_compose_environment(
            service, project=project, port=port, fixture_scope=fixture_scope
        )
        _require(actual == expected, f"compose_environment_mismatch:{service}")


def validate_runtime_container_environment(
    inspected: Mapping[str, object],
    *,
    service: str,
    baseline: ImageEnvironmentBaseline,
    project: str,
    port: int,
    fixture_scope: str | None = None,
) -> None:
    """Bind a container's exact environment to its verified immutable image baseline."""

    _validate_context(project, port, fixture_scope)
    _require(service in SERVICES, "unexpected_environment_service")
    required_kind = "web" if service == "web" else "backend"
    _require(
        baseline.image_kind == required_kind
        and bool(_IMAGE_ID_PATTERN.fullmatch(baseline.image_id))
        and dict(baseline.environment) == _EXPECTED_IMAGE_ENVIRONMENTS[required_kind]
        and len(baseline.environment) == len(dict(baseline.environment)),
        "invalid_image_environment_baseline",
    )
    _require(isinstance(inspected, Mapping), "malformed_runtime_inspection")
    _require(inspected.get("Image") == baseline.image_id, "runtime_image_identity_mismatch")
    config = inspected.get("Config")
    _require(isinstance(config, Mapping), "malformed_runtime_inspection")
    actual = _environment_entries(config.get("Env"), source="runtime")
    expected = dict(baseline.environment)
    expected.update(
        _expected_compose_environment(
            service, project=project, port=port, fixture_scope=fixture_scope
        )
    )
    _require(actual == expected, f"runtime_environment_mismatch:{service}")
